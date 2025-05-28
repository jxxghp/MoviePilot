import asyncio
import io
import json
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Annotated, List

import aiofiles
import pillow_avif  # noqa 用于自动注册AVIF支持
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import schemas
from app.chain.search import SearchChain
from app.chain.system import SystemChain
from app.core.config import global_vars, settings
from app.core.metainfo import MetaInfo
from app.core.module import ModuleManager
from app.core.security import verify_apitoken, verify_resource_token, verify_token
from app.db.models import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_superuser
from app.helper.mediaserver import MediaServerHelper
from app.helper.message import MessageHelper, MessageQueueManager
from app.helper.progress import ProgressHelper
from app.helper.rule import RuleHelper
from app.helper.sites import SitesHelper
from app.helper.subscribe import SubscribeHelper
from app.log import logger
from app.monitor import Monitor
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey, MediaType
from app.utils.crypto import HashUtils
from app.utils.http import RequestUtils
from app.utils.security import SecurityUtils
from app.utils.url import UrlUtils
from app.helper.system import SystemHelper
from version import APP_VERSION

router = APIRouter()


def fetch_image(
        url: str,
        proxy: bool = False,
        use_disk_cache: bool = False,
        if_none_match: Optional[str] = None,
        allowed_domains: Optional[set[str]] = None) -> Response:
    """
    处理图片缓存逻辑，支持HTTP缓存和磁盘缓存
    """
    if not url:
        raise HTTPException(status_code=404, detail="URL not provided")

    if allowed_domains is None:
        allowed_domains = set(settings.SECURITY_IMAGE_DOMAINS)

    # 验证URL安全性
    if not SecurityUtils.is_safe_url(url, allowed_domains):
        raise HTTPException(status_code=404, detail="Unsafe URL")

    # 后续观察系统性能表现，如果发现磁盘缓存和HTTP缓存无法满足高并发情况下的响应速度需求，可以考虑重新引入内存缓存
    cache_path = None
    if use_disk_cache:
        # 生成缓存路径
        sanitized_path = SecurityUtils.sanitize_url_path(url)
        cache_path = settings.CACHE_PATH / "images" / sanitized_path

        # 没有文件类型，则添加后缀，在恶意文件类型和实际需求下的折衷选择
        if not cache_path.suffix:
            cache_path = cache_path.with_suffix(".jpg")

        # 确保缓存路径和文件类型合法
        if not SecurityUtils.is_safe_path(settings.CACHE_PATH, cache_path, settings.SECURITY_IMAGE_SUFFIXES):
            raise HTTPException(status_code=400, detail="Invalid cache path or file type")

        # 目前暂不考虑磁盘缓存文件是否过期，后续通过缓存清理机制处理
        if cache_path.exists():
            try:
                content = cache_path.read_bytes()
                etag = HashUtils.md5(content)
                headers = RequestUtils.generate_cache_headers(etag, max_age=86400 * 7)
                if if_none_match == etag:
                    return Response(status_code=304, headers=headers)
                return Response(content=content, media_type="image/jpeg", headers=headers)
            except Exception as e:
                # 如果读取磁盘缓存发生异常，这里仅记录日志，尝试再次请求远端进行处理
                logger.debug(f"Failed to read cache file {cache_path}: {e}")

    # 请求远程图片
    referer = "https://movie.douban.com/" if "doubanio.com" in url else None
    proxies = settings.PROXY if proxy else None
    response = RequestUtils(ua=settings.USER_AGENT, proxies=proxies, referer=referer,
                            accept_type="image/avif,image/webp,image/apng,*/*").get_res(url=url)
    if not response:
        raise HTTPException(status_code=502, detail="Failed to fetch the image from the remote server")

    # 验证下载的内容是否为有效图片
    try:
        Image.open(io.BytesIO(response.content)).verify()
    except Exception as e:
        logger.debug(f"Invalid image format for URL {url}: {e}")
        raise HTTPException(status_code=502, detail="Invalid image format")

    content = response.content
    response_headers = response.headers

    cache_control_header = response_headers.get("Cache-Control", "")
    cache_directive, max_age = RequestUtils.parse_cache_control(cache_control_header)

    # 如果需要使用磁盘缓存，则保存到磁盘
    if use_disk_cache and cache_path:
        try:
            if not cache_path.parent.exists():
                cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as tmp_file:
                tmp_file.write(content)
                temp_path = Path(tmp_file.name)
            temp_path.replace(cache_path)
        except Exception as e:
            logger.debug(f"Failed to write cache file {cache_path}: {e}")

    # 检查 If-None-Match
    etag = HashUtils.md5(content)
    if if_none_match == etag:
        headers = RequestUtils.generate_cache_headers(etag, cache_directive, max_age)
        return Response(status_code=304, headers=headers)

    headers = RequestUtils.generate_cache_headers(etag, cache_directive, max_age)

    return Response(
        content=content,
        media_type=response_headers.get("Content-Type") or UrlUtils.get_mime_type(url, "image/jpeg"),
        headers=headers
    )


@router.get("/img/{proxy}", summary="图片代理")
def proxy_img(
        imgurl: str,
        proxy: bool = False,
        if_none_match: Annotated[str | None, Header()] = None,
        _: schemas.TokenPayload = Depends(verify_resource_token)
) -> Response:
    """
    图片代理，可选是否使用代理服务器，支持 HTTP 缓存
    """
    # 媒体服务器添加图片代理支持
    hosts = [config.config.get("host") for config in MediaServerHelper().get_configs().values() if
             config and config.config and config.config.get("host")]
    allowed_domains = set(settings.SECURITY_IMAGE_DOMAINS) | set(hosts)
    return fetch_image(url=imgurl, proxy=proxy, use_disk_cache=False,
                       if_none_match=if_none_match, allowed_domains=allowed_domains)


@router.get("/cache/image", summary="图片缓存")
def cache_img(
        url: str,
        if_none_match: Annotated[str | None, Header()] = None,
        _: schemas.TokenPayload = Depends(verify_resource_token)
) -> Response:
    """
    本地缓存图片文件，支持 HTTP 缓存，如果启用全局图片缓存，则使用磁盘缓存
    """
    # 如果没有启用全局图片缓存，则不使用磁盘缓存
    proxy = "doubanio.com" not in url
    return fetch_image(url=url, proxy=proxy, use_disk_cache=settings.GLOBAL_IMAGE_CACHE, if_none_match=if_none_match)


@router.get("/global", summary="查询非敏感系统设置", response_model=schemas.Response)
def get_global_setting(token: str):
    """
    查询非敏感系统设置（默认鉴权）
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")

    # FIXME: 新增敏感配置项时要在此处添加排除项
    info = settings.dict(
        exclude={"SECRET_KEY", "RESOURCE_SECRET_KEY", "API_TOKEN", "TMDB_API_KEY", "TVDB_API_KEY", "FANART_API_KEY",
                 "COOKIECLOUD_KEY", "COOKIECLOUD_PASSWORD", "GITHUB_TOKEN", "REPO_GITHUB_TOKEN"}
    )
    # 追加用户唯一ID和订阅分享管理权限
    info.update({
        "USER_UNIQUE_ID": SubscribeHelper().get_user_uuid(),
        "SUBSCRIBE_SHARE_MANAGE": SubscribeHelper().is_admin_user(),
    })
    return schemas.Response(success=True,
                            data=info)


@router.get("/env", summary="查询系统配置", response_model=schemas.Response)
def get_env_setting(_: User = Depends(get_current_active_superuser)):
    """
    查询系统环境变量，包括当前版本号（仅管理员）
    """
    info = settings.dict(
        exclude={"SECRET_KEY", "RESOURCE_SECRET_KEY"}
    )
    info.update({
        "VERSION": APP_VERSION,
        "AUTH_VERSION": SitesHelper().auth_version,
        "INDEXER_VERSION": SitesHelper().indexer_version,
        "FRONTEND_VERSION": SystemChain().get_frontend_version()
    })
    return schemas.Response(success=True,
                            data=info)


@router.post("/env", summary="更新系统配置", response_model=schemas.Response)
def set_env_setting(env: dict,
                    _: User = Depends(get_current_active_superuser)):
    """
    更新系统环境变量（仅管理员）
    """
    result = settings.update_settings(env=env)
    # 统计成功和失败的结果
    success_updates = {k: v for k, v in result.items() if v[0]}
    failed_updates = {k: v for k, v in result.items() if not v[0]}

    if failed_updates:
        return schemas.Response(
            success=False,
            message="部分配置项更新失败",
            data={
                "success_updates": success_updates,
                "failed_updates": failed_updates
            }
        )

    return schemas.Response(
        success=True,
        message="所有配置项更新成功",
        data={
            "success_updates": success_updates
        }
    )


@router.get("/progress/{process_type}", summary="实时进度")
async def get_progress(request: Request, process_type: str, _: schemas.TokenPayload = Depends(verify_resource_token)):
    """
    实时获取处理进度，返回格式为SSE
    """
    progress = ProgressHelper()

    async def event_generator():
        try:
            while not global_vars.is_system_stopped:
                if await request.is_disconnected():
                    break
                detail = progress.get(process_type)
                yield f"data: {json.dumps(detail)}\n\n"
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/setting/{key}", summary="查询系统设置", response_model=schemas.Response)
def get_setting(key: str,
                _: User = Depends(get_current_active_superuser)):
    """
    查询系统设置（仅管理员）
    """
    if hasattr(settings, key):
        value = getattr(settings, key)
    else:
        value = SystemConfigOper().get(key)
    return schemas.Response(success=True, data={
        "value": value
    })


@router.post("/setting/{key}", summary="更新系统设置", response_model=schemas.Response)
def set_setting(key: str, value: Union[list, dict, bool, int, str] = None,
                _: User = Depends(get_current_active_superuser)):
    """
    更新系统设置（仅管理员）
    """
    if hasattr(settings, key):
        success, message = settings.update_setting(key=key, value=value)
        return schemas.Response(success=success, message=message)
    elif key in {item.value for item in SystemConfigKey}:
        if isinstance(value, list):
            value = list(filter(None, value))
            value = value if value else None
        SystemConfigOper().set(key, value)
        return schemas.Response(success=True)
    else:
        return schemas.Response(success=False, message=f"配置项 '{key}' 不存在")


@router.get("/message", summary="实时消息")
async def get_message(request: Request, role: Optional[str] = "system",
                      _: schemas.TokenPayload = Depends(verify_resource_token)):
    """
    实时获取系统消息，返回格式为SSE
    """
    message = MessageHelper()

    async def event_generator():
        try:
            while not global_vars.is_system_stopped:
                if await request.is_disconnected():
                    break
                detail = message.get(role)
                yield f"data: {detail or ''}\n\n"
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/logging", summary="实时日志")
async def get_logging(request: Request, length: Optional[int] = 50, logfile: Optional[str] = "moviepilot.log",
                      _: schemas.TokenPayload = Depends(verify_resource_token)):
    """
    实时获取系统日志
    length = -1 时, 返回text/plain
    否则 返回格式SSE
    """
    log_path = settings.LOG_PATH / logfile

    if not SecurityUtils.is_safe_path(settings.LOG_PATH, log_path, allowed_suffixes={".log"}):
        raise HTTPException(status_code=404, detail="Not Found")

    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")

    async def log_generator():
        try:
            # 使用固定大小的双向队列来限制内存使用
            lines_queue = deque(maxlen=max(length, 50))
            # 使用 aiofiles 异步读取文件
            async with aiofiles.open(log_path, mode="r", encoding="utf-8") as f:
                # 逐行读取文件，将每一行存入队列
                file_content = await f.read()
                for line in file_content.splitlines():
                    lines_queue.append(line)
                for line in lines_queue:
                    yield f"data: {line}\n\n"
                # 移动文件指针到文件末尾，继续监听新增内容
                await f.seek(0, 2)
                while not global_vars.is_system_stopped:
                    if await request.is_disconnected():
                        break
                    line = await f.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            return

    # 根据length参数返回不同的响应
    if length == -1:
        # 返回全部日志作为文本响应
        if not log_path.exists():
            return Response(content="日志文件不存在！", media_type="text/plain")
        with open(log_path, "r", encoding='utf-8') as file:
            text = file.read()
        # 倒序输出
        text = "\n".join(text.split("\n")[::-1])
        return Response(content=text, media_type="text/plain")
    else:
        # 返回SSE流响应
        return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get("/versions", summary="查询Github所有Release版本", response_model=schemas.Response)
def latest_version(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询Github所有Release版本
    """
    version_res = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS).get_res(
        f"https://api.github.com/repos/jxxghp/MoviePilot/releases")
    if version_res:
        ver_json = version_res.json()
        if ver_json:
            return schemas.Response(success=True, data=ver_json)
    return schemas.Response(success=False)


@router.get("/ruletest", summary="过滤规则测试", response_model=schemas.Response)
def ruletest(title: str,
             rulegroup_name: str,
             subtitle: Optional[str] = None,
             _: schemas.TokenPayload = Depends(verify_token)):
    """
    过滤规则测试，规则类型 1-订阅，2-洗版，3-搜索
    """
    torrent = schemas.TorrentInfo(
        title=title,
        description=subtitle,
    )
    # 查询规则组详情
    rulegroup = RuleHelper().get_rule_group(rulegroup_name)
    if not rulegroup:
        return schemas.Response(success=False, message=f"过滤规则组 {rulegroup_name} 不存在！")

    # 根据标题查询媒体信息
    media_info = SearchChain().recognize_media(MetaInfo(title=title, subtitle=subtitle))
    if not media_info:
        return schemas.Response(success=False, message="未识别到媒体信息！")

    # 过滤
    result = SearchChain().filter_torrents(rule_groups=[rulegroup.name],
                                           torrent_list=[torrent], mediainfo=media_info)
    if not result:
        return schemas.Response(success=False, message="不符合过滤规则！")
    return schemas.Response(success=True, data={
        "priority": 100 - result[0].pri_order + 1
    })


@router.get("/nettest", summary="测试网络连通性")
def nettest(url: str,
            proxy: bool,
            _: schemas.TokenPayload = Depends(verify_token)):
    """
    测试网络连通性
    """
    # 记录开始的毫秒数
    start_time = datetime.now()
    url = url.replace("{TMDBAPIKEY}", settings.TMDB_API_KEY)
    result = RequestUtils(proxies=settings.PROXY if proxy else None,
                          ua=settings.USER_AGENT).get_res(url)
    # 计时结束的毫秒数
    end_time = datetime.now()
    # 计算相关秒数
    if result and result.status_code == 200:
        return schemas.Response(success=True, data={
            "time": round((end_time - start_time).microseconds / 1000)
        })
    elif result:
        return schemas.Response(success=False, message=f"错误码：{result.status_code}", data={
            "time": round((end_time - start_time).microseconds / 1000)
        })
    else:
        return schemas.Response(success=False, message="网络连接失败！")


@router.get("/modulelist", summary="查询已加载的模块ID列表", response_model=schemas.Response)
def modulelist(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询已加载的模块ID列表
    """
    modules = [{
        "id": k,
        "name": v.get_name(),
    } for k, v in ModuleManager().get_modules().items()]
    return schemas.Response(success=True, data={
        "modules": modules
    })


@router.get("/moduletest/{moduleid}", summary="模块可用性测试", response_model=schemas.Response)
def moduletest(moduleid: str, _: schemas.TokenPayload = Depends(verify_token)):
    """
    模块可用性测试接口
    """
    state, errmsg = ModuleManager().test(moduleid)
    return schemas.Response(success=state, message=errmsg)


@router.get("/restart", summary="重启系统", response_model=schemas.Response)
def restart_system(_: User = Depends(get_current_active_superuser)):
    """
    重启系统（仅管理员）
    """
    if not SystemHelper.can_restart():
        return schemas.Response(success=False, message="当前运行环境不支持重启操作！")
    # 标识停止事件
    global_vars.stop_system()
    # 执行重启
    ret, msg = SystemHelper.restart()
    return schemas.Response(success=ret, message=msg)


@router.get("/reload", summary="重新加载模块", response_model=schemas.Response)
def reload_module(_: User = Depends(get_current_active_superuser)):
    """
    重新加载模块（仅管理员）
    """
    MessageQueueManager().init_config()
    ModuleManager().reload()
    Scheduler().init()
    Monitor().init()
    return schemas.Response(success=True)


@router.get("/runscheduler", summary="运行服务", response_model=schemas.Response)
def run_scheduler(jobid: str,
                  _: User = Depends(get_current_active_superuser)):
    """
    执行命令（仅管理员）
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")
    Scheduler().start(jobid)
    return schemas.Response(success=True)


@router.get("/runscheduler2", summary="运行服务（API_TOKEN）", response_model=schemas.Response)
def run_scheduler2(jobid: str,
                   _: Annotated[str, Depends(verify_apitoken)]):
    """
    执行命令（API_TOKEN认证）
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")

    Scheduler().start(jobid)
    return schemas.Response(success=True)


@router.get("/sites/mapping", summary="获取站点域名到名称的映射", response_model=schemas.Response)
def get_sites_mapping(_: User = Depends(get_current_active_superuser)):
    """
    获取站点域名到名称的映射关系
    """
    try:
        from app.db.site_oper import SiteOper
        
        site_oper = SiteOper()
        sites = site_oper.list()
        
        mapping = {}
        for site in sites:
            mapping[site.domain] = site.name
        
        return schemas.Response(success=True, data=mapping)
    except Exception as e:
        logger.error(f"获取站点映射失败：{str(e)}")
        return schemas.Response(success=False, message=f"获取映射失败：{str(e)}")


@router.get("/cache/torrents", summary="获取种子缓存", response_model=schemas.Response)
def get_torrents_cache(_: User = Depends(get_current_active_superuser)):
    """
    获取当前种子缓存数据
    """
    from app.chain.torrents import TorrentsChain
    
    torrents_chain = TorrentsChain()
    
    # 获取spider和rss两种缓存
    spider_cache = torrents_chain.get_torrents("spider")
    rss_cache = torrents_chain.get_torrents("rss")
    
    # 统计信息
    spider_count = sum(len(torrents) for torrents in spider_cache.values())
    rss_count = sum(len(torrents) for torrents in rss_cache.values())
    
    # 转换为前端需要的格式
    spider_data = []
    for domain, contexts in spider_cache.items():
        for context in contexts:
            torrent_hash = HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}")
            spider_data.append({
                "hash": torrent_hash,
                "domain": domain,
                "title": context.torrent_info.title,
                "description": context.torrent_info.description,
                "size": context.torrent_info.size,
                "pubdate": context.torrent_info.pubdate,
                "site_name": context.torrent_info.site_name,
                "media_name": context.media_info.title if context.media_info else "",
                "media_year": context.media_info.year if context.media_info else "",
                "media_type": context.media_info.type if context.media_info else "",
                "season_episode": context.meta_info.season_episode if context.meta_info else "",
                "resource_term": context.meta_info.resource_term if context.meta_info else "",
                "enclosure": context.torrent_info.enclosure,
                "page_url": context.torrent_info.page_url,
                "poster_path": context.media_info.get_poster_image() if context.media_info else "",
                "backdrop_path": context.media_info.get_backdrop_image() if context.media_info else ""
            })
    
    rss_data = []
    for domain, contexts in rss_cache.items():
        for context in contexts:
            torrent_hash = HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}")
            rss_data.append({
                "hash": torrent_hash,
                "domain": domain,
                "title": context.torrent_info.title,
                "description": context.torrent_info.description,
                "size": context.torrent_info.size,
                "pubdate": context.torrent_info.pubdate,
                "site_name": context.torrent_info.site_name,
                "media_name": context.media_info.title if context.media_info else "",
                "media_year": context.media_info.year if context.media_info else "",
                "media_type": context.media_info.type if context.media_info else "",
                "season_episode": context.meta_info.season_episode if context.meta_info else "",
                "resource_term": context.meta_info.resource_term if context.meta_info else "",
                "enclosure": context.torrent_info.enclosure,
                "page_url": context.torrent_info.page_url,
                "poster_path": context.media_info.get_poster_image() if context.media_info else "",
                "backdrop_path": context.media_info.get_backdrop_image() if context.media_info else ""
            })
    
    return schemas.Response(success=True, data={
        "spider": {
            "count": spider_count,
            "sites": len(spider_cache),
            "data": spider_data
        },
        "rss": {
            "count": rss_count,
            "sites": len(rss_cache),
            "data": rss_data
        }
    })


@router.post("/cache/torrents/refresh", summary="刷新种子缓存", response_model=schemas.Response)
def refresh_torrents_cache(cache_type: str = "auto", _: User = Depends(get_current_active_superuser)):
    """
    刷新种子缓存
    :param cache_type: 缓存类型 auto/spider/rss
    """
    from app.chain.torrents import TorrentsChain
    
    torrents_chain = TorrentsChain()
    
    try:
        if cache_type == "auto":
            cache_type = None
        
        result = torrents_chain.refresh(stype=cache_type, sites=None)
        
        # 统计刷新结果
        total_count = sum(len(torrents) for torrents in result.values())
        sites_count = len(result)
        
        return schemas.Response(success=True, message=f"缓存刷新完成，共刷新 {sites_count} 个站点，{total_count} 个种子")
    except Exception as e:
        logger.error(f"刷新种子缓存失败：{str(e)}")
        return schemas.Response(success=False, message=f"刷新失败：{str(e)}")


@router.delete("/cache/torrents", summary="清理种子缓存", response_model=schemas.Response)
def clear_torrents_cache(_: User = Depends(get_current_active_superuser)):
    """
    清理所有种子缓存
    """
    from app.chain.torrents import TorrentsChain
    
    torrents_chain = TorrentsChain()
    
    try:
        torrents_chain.clear_torrents()
        return schemas.Response(success=True, message="种子缓存清理完成")
    except Exception as e:
        logger.error(f"清理种子缓存失败：{str(e)}")
        return schemas.Response(success=False, message=f"清理失败：{str(e)}")


@router.get("/cache/torrents/stats", summary="获取种子缓存统计", response_model=schemas.Response)
def get_torrents_cache_stats(_: User = Depends(get_current_active_superuser)):
    """
    获取种子缓存统计信息
    """
    from app.chain.torrents import TorrentsChain
    
    torrents_chain = TorrentsChain()
    
    # 获取缓存配置
    cache_limit = settings.CACHE_CONF.get("torrents", 100)
    refresh_limit = settings.CACHE_CONF.get("refresh", 30)
    
    # 获取缓存数据
    spider_cache = torrents_chain.get_torrents("spider")
    rss_cache = torrents_chain.get_torrents("rss")
    
    # 统计各站点缓存情况
    spider_stats = []
    for domain, contexts in spider_cache.items():
        spider_stats.append({
            "domain": domain,
            "count": len(contexts),
            "latest_date": max([ctx.torrent_info.pubdate for ctx in contexts if ctx.torrent_info.pubdate], default="")
        })
    
    rss_stats = []
    for domain, contexts in rss_cache.items():
        rss_stats.append({
            "domain": domain,
            "count": len(contexts),
            "latest_date": max([ctx.torrent_info.pubdate for ctx in contexts if ctx.torrent_info.pubdate], default="")
        })
    
    return schemas.Response(success=True, data={
        "config": {
            "cache_limit": cache_limit,
            "refresh_limit": refresh_limit,
            "current_mode": settings.SUBSCRIBE_MODE
        },
        "spider": {
            "total_count": sum(len(torrents) for torrents in spider_cache.values()),
            "sites_count": len(spider_cache),
            "sites": spider_stats
        },
        "rss": {
            "total_count": sum(len(torrents) for torrents in rss_cache.values()),
            "sites_count": len(rss_cache),
            "sites": rss_stats
        }
    })


@router.delete("/cache/torrents/{cache_type}/{domain}/{torrent_hash}", summary="删除指定种子缓存", response_model=schemas.Response)
def delete_torrent_cache(cache_type: str, domain: str, torrent_hash: str, 
                        _: User = Depends(get_current_active_superuser)):
    """
    删除指定的种子缓存
    :param cache_type: 缓存类型 spider/rss
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    """
    from app.chain.torrents import TorrentsChain
    from app.utils.crypto import HashUtils
    
    torrents_chain = TorrentsChain()
    
    try:
        # 获取当前缓存
        cache_data = torrents_chain.get_torrents(cache_type)
        
        if domain not in cache_data:
            return schemas.Response(success=False, message=f"站点 {domain} 缓存不存在")
        
        # 查找并删除指定种子
        original_count = len(cache_data[domain])
        cache_data[domain] = [
            context for context in cache_data[domain]
            if HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}") != torrent_hash
        ]
        
        if len(cache_data[domain]) == original_count:
            return schemas.Response(success=False, message="未找到指定的种子")
        
        # 保存更新后的缓存
        if cache_type == "spider":
            torrents_chain.save_cache(cache_data, torrents_chain._spider_file)
        else:
            torrents_chain.save_cache(cache_data, torrents_chain._rss_file)
        
        return schemas.Response(success=True, message="种子删除成功")
    except Exception as e:
        logger.error(f"删除种子缓存失败：{str(e)}")
        return schemas.Response(success=False, message=f"删除失败：{str(e)}")


@router.post("/cache/torrents/{cache_type}/{domain}/{torrent_hash}/reidentify", summary="重新识别种子", response_model=schemas.Response)
def reidentify_torrent_cache(cache_type: str, domain: str, torrent_hash: str,
                            tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                            _: User = Depends(get_current_active_superuser)):
    """
    重新识别指定的种子
    :param cache_type: 缓存类型 spider/rss
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    :param tmdbid: 手动指定的TMDB ID
    :param doubanid: 手动指定的豆瓣ID
    """
    from app.chain.torrents import TorrentsChain
    from app.chain.media import MediaChain
    from app.core.metainfo import MetaInfo
    from app.core.context import MediaInfo
    from app.utils.crypto import HashUtils
    from app.schemas.types import MediaType
    
    torrents_chain = TorrentsChain()
    media_chain = MediaChain()
    
    try:
        # 获取当前缓存
        cache_data = torrents_chain.get_torrents(cache_type)
        
        if domain not in cache_data:
            return schemas.Response(success=False, message=f"站点 {domain} 缓存不存在")
        
        # 查找指定种子
        target_context = None
        for context in cache_data[domain]:
            if HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}") == torrent_hash:
                target_context = context
                break
        
        if not target_context:
            return schemas.Response(success=False, message="未找到指定的种子")
        
        # 重新识别
        if tmdbid or doubanid:
            # 手动指定媒体信息
            if tmdbid:
                # 先尝试电影类型
                tmdbinfo = media_chain.tmdb_info(tmdbid=tmdbid, mtype=MediaType.MOVIE)
                if not tmdbinfo:
                    # 再尝试电视剧类型
                    tmdbinfo = media_chain.tmdb_info(tmdbid=tmdbid, mtype=MediaType.TV)
                
                if tmdbinfo:
                    mediainfo = MediaInfo()
                    mediainfo.set_tmdb_info(tmdbinfo)
                else:
                    mediainfo = None
            else:
                # 先尝试电影类型
                doubaninfo = media_chain.douban_info(doubanid=doubanid, mtype=MediaType.MOVIE)
                if not doubaninfo:
                    # 再尝试电视剧类型
                    doubaninfo = media_chain.douban_info(doubanid=doubanid, mtype=MediaType.TV)
                
                if doubaninfo:
                    mediainfo = MediaInfo()
                    mediainfo.set_douban_info(doubaninfo)
                else:
                    mediainfo = None
        else:
            # 自动重新识别
            meta = MetaInfo(title=target_context.torrent_info.title, 
                          subtitle=target_context.torrent_info.description)
            mediainfo = media_chain.recognize_by_meta(meta)
        
        if not mediainfo:
            # 创建空的媒体信息
            mediainfo = MediaInfo()
        else:
            # 清理多余数据
            mediainfo.clear()
        
        # 更新上下文中的媒体信息
        target_context.media_info = mediainfo
        
        # 保存更新后的缓存
        if cache_type == "spider":
            torrents_chain.save_cache(cache_data, torrents_chain._spider_file)
        else:
            torrents_chain.save_cache(cache_data, torrents_chain._rss_file)
        
        return schemas.Response(success=True, message="重新识别完成", data={
            "media_name": mediainfo.title if mediainfo else "",
            "media_year": mediainfo.year if mediainfo else "",
            "media_type": mediainfo.type.value if mediainfo and mediainfo.type else ""
        })
    except Exception as e:
        logger.error(f"重新识别种子失败：{str(e)}")
        return schemas.Response(success=False, message=f"重新识别失败：{str(e)}")


@router.get("/cache/images/stats", summary="获取图片缓存统计", response_model=schemas.Response)
def get_images_cache_stats(_: User = Depends(get_current_active_superuser)):
    """
    获取图片缓存统计信息
    """
    import os
    from pathlib import Path
    
    try:
        images_cache_path = settings.CACHE_PATH / "images"
        
        if not images_cache_path.exists():
            return schemas.Response(success=True, data={
                "total_files": 0,
                "total_size": 0,
                "cache_enabled": settings.GLOBAL_IMAGE_CACHE
            })
        
        total_files = 0
        total_size = 0
        
        # 递归统计所有图片文件
        for root, dirs, files in os.walk(images_cache_path):
            for file in files:
                file_path = Path(root) / file
                if file_path.suffix.lower() in settings.SECURITY_IMAGE_SUFFIXES:
                    total_files += 1
                    try:
                        total_size += file_path.stat().st_size
                    except (OSError, IOError):
                        continue
        
        return schemas.Response(success=True, data={
            "total_files": total_files,
            "total_size": total_size,
            "cache_enabled": settings.GLOBAL_IMAGE_CACHE,
            "cache_path": str(images_cache_path)
        })
    except Exception as e:
        logger.error(f"获取图片缓存统计失败：{str(e)}")
        return schemas.Response(success=False, message=f"获取统计失败：{str(e)}")


@router.delete("/cache/images", summary="清理图片缓存", response_model=schemas.Response)
def clear_images_cache(_: User = Depends(get_current_active_superuser)):
    """
    清理所有图片缓存
    """
    try:
        from app.utils.system import SystemUtils
        
        images_cache_path = settings.CACHE_PATH / "images"
        
        if not images_cache_path.exists():
            return schemas.Response(success=True, message="图片缓存目录不存在")
        
        # 清理图片缓存目录
        cleared_count = SystemUtils.clear(images_cache_path, days=0)
        
        return schemas.Response(success=True, message=f"图片缓存清理完成，清理了 {cleared_count} 个文件")
    except Exception as e:
        logger.error(f"清理图片缓存失败：{str(e)}")
        return schemas.Response(success=False, message=f"清理失败：{str(e)}")


@router.post("/cache/images/clean", summary="清理过期图片缓存", response_model=schemas.Response)
def clean_expired_images_cache(days: int = 7, _: User = Depends(get_current_active_superuser)):
    """
    清理过期的图片缓存
    :param days: 保留天数，默认7天
    """
    try:
        from app.utils.system import SystemUtils
        
        images_cache_path = settings.CACHE_PATH / "images"
        
        if not images_cache_path.exists():
            return schemas.Response(success=True, message="图片缓存目录不存在")
        
        # 清理过期图片缓存
        cleared_count = SystemUtils.clear(images_cache_path, days=days)
        
        return schemas.Response(success=True, message=f"过期图片缓存清理完成，清理了 {cleared_count} 个文件")
    except Exception as e:
        logger.error(f"清理过期图片缓存失败：{str(e)}")
        return schemas.Response(success=False, message=f"清理失败：{str(e)}")
