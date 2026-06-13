import asyncio
import io
import json
import re
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union, Annotated
from urllib.parse import urljoin, urlparse

import aiofiles
import pillow_avif  # noqa 用于自动注册AVIF支持
from anyio import Path as AsyncPath
from app.helper.sites import SitesHelper  # noqa  # noqa
from fastapi import APIRouter, Body, Depends, HTTPException, Header, Request, Response
from fastapi.responses import StreamingResponse

from app import schemas
from app.chain.media import MediaChain
from app.chain.mediaserver import MediaServerChain
from app.chain.search import SearchChain
from app.chain.system import SystemChain
from app.core.config import global_vars, settings
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.module import ModuleManager
from app.core.security import verify_apitoken, verify_resource_token, verify_token
from app.db.models import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import (
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.helper.image import ImageHelper
from app.helper.message import MessageHelper
from app.helper.server import MoviePilotServerHelper
from app.helper.progress import ProgressHelper
from app.helper.rule import RuleHelper
from app.helper.system import SystemHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas import ConfigChangeEventData
from app.schemas.types import SystemConfigKey, EventType
from app.utils.crypto import HashUtils
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils import rust_accel
from app.utils.security import SecurityUtils
from app.utils.url import UrlUtils
from version import APP_VERSION

router = APIRouter()

_NETTEST_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_PUBLIC_SYSTEM_CONFIG_KEYS = {
    item.value: item
    for item in (
        SystemConfigKey.Directories,
        SystemConfigKey.Storages,
        SystemConfigKey.IndexerSites,
        SystemConfigKey.EpisodeFormatRuleTable,
        SystemConfigKey.DefaultMovieSubscribeConfig,
        SystemConfigKey.DefaultTvSubscribeConfig,
        SystemConfigKey.FollowSubscribers,
    )
}
_PUBLIC_SETTINGS_KEYS = {"PLUGIN_MARKET"}
_LOG_DOWNLOAD_LIMIT = 10
_LOG_DOWNLOAD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _match_nettest_prefix(url: str, prefix: str) -> bool:
    """
    判断目标URL是否仍然落在允许的协议、主机、端口和路径前缀内。

    nettest 会在服务端手动处理重定向，因此这里需要一个比简单 startswith
    更严格的匹配，避免不同端口或同名路径被误判为白名单内跳转。
    """
    parsed_url = urlparse(url)
    parsed_prefix = urlparse(prefix)
    if parsed_url.scheme.lower() != parsed_prefix.scheme.lower():
        return False
    if (parsed_url.hostname or "").lower() != (parsed_prefix.hostname or "").lower():
        return False
    url_port = parsed_url.port or (443 if parsed_url.scheme.lower() == "https" else 80)
    prefix_port = parsed_prefix.port or (
        443 if parsed_prefix.scheme.lower() == "https" else 80
    )
    if url_port != prefix_port:
        return False
    return parsed_url.path.startswith(parsed_prefix.path or "/")


def _build_nettest_rules() -> list[dict[str, Any]]:
    """
    构建系统内置的网络测试目标。

    这里集中维护“前端允许显示哪些测试项”和“后端允许访问哪些远端地址”。
    前端只拿到展示所需的 id/name/icon；真正的 URL、代理策略、内容校验规则
    和重定向白名单都保留在服务端，避免再出现用户可控 SSRF。
    """
    github_proxy = UrlUtils.standardize_base_url(settings.GITHUB_PROXY or "")
    pip_proxy = UrlUtils.standardize_base_url(
        settings.PIP_PROXY or "https://pypi.org/simple/"
    )
    tmdb_key = settings.TMDB_API_KEY
    tmdb_domain = settings.TMDB_API_DOMAIN or "api.themoviedb.org"

    github_readme_url = "https://github.com/jxxghp/MoviePilot/blob/v2/README.md"
    raw_readme_url = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/README.md"

    rules = [
        {
            "id": "tmdb_api",
            "name": "api.themoviedb.org",
            "icon": "tmdb",
            "url": f"https://api.themoviedb.org/3/movie/550?api_key={tmdb_key}",
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://api.themoviedb.org/3/",
            ],
        },
        {
            "id": "tmdb_api_alt",
            "name": "api.tmdb.org",
            "icon": "tmdb",
            "url": f"https://api.tmdb.org/3/movie/550?api_key={tmdb_key}",
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://api.tmdb.org/3/",
            ],
        },
        {
            "id": "tmdb_web",
            "name": "www.themoviedb.org",
            "icon": "tmdb",
            "url": "https://www.themoviedb.org",
            "proxy": True,
            "allowed_redirect_prefixes": ["https://www.themoviedb.org/"],
        },
        {
            "id": "tvdb_api",
            "name": "api.thetvdb.com",
            "icon": "tvdb",
            "url": "https://api.thetvdb.com/series/81189",
            "proxy": True,
            "allowed_redirect_prefixes": ["https://api.thetvdb.com/"],
        },
        {
            "id": "fanart_api",
            "name": "webservice.fanart.tv",
            "icon": "fanart",
            "url": "https://webservice.fanart.tv",
            "proxy": True,
            "allowed_redirect_prefixes": ["https://webservice.fanart.tv/"],
        },
        {
            "id": "telegram_api",
            "name": "api.telegram.org",
            "icon": "telegram",
            "url": "https://api.telegram.org",
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://api.telegram.org/",
                "https://core.telegram.org/",
            ],
        },
        {
            "id": "wechat_api",
            "name": "qyapi.weixin.qq.com",
            "icon": "wechat",
            "url": "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            "proxy": False,
            "allowed_redirect_prefixes": ["https://qyapi.weixin.qq.com/"],
        },
        {
            "id": "douban_api",
            "name": "frodo.douban.com",
            "icon": "douban",
            "url": "https://frodo.douban.com",
            "proxy": False,
            "allowed_redirect_prefixes": [
                "https://frodo.douban.com/",
                "https://www.douban.com/doubanapp/frodo",
            ],
        },
        {
            "id": "slack_api",
            "name": "slack.com",
            "icon": "slack",
            "url": "https://slack.com",
            "proxy": False,
            "allowed_redirect_prefixes": [
                "https://slack.com/",
                "https://www.slack.com/",
            ],
        },
        {
            "id": "pip_proxy",
            "name": "pypi.org",
            "icon": "python",
            "url": f"{pip_proxy}rsa/",
            "proxy": True,
            "allowed_redirect_prefixes": [
                pip_proxy,
                "https://pypi.org/simple/",
            ],
            "expected_text": "pypi:repository-version",
            "invalid_message": "PIP加速代理已失效，请检查配置",
            "proxy_name": "PIP加速代理",
        },
        {
            "id": "github_proxy_web",
            "name": "github.com",
            "icon": "github",
            "url": f"{github_proxy}{github_readme_url}"
            if github_proxy
            else github_readme_url,
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://github.com/",
                *((f"{github_proxy}https://github.com/",) if github_proxy else ()),
            ],
            "expected_text": "MoviePilot",
            "invalid_message": "Github加速代理已失效，请检查配置"
            if github_proxy
            else "无效响应",
            "proxy_name": "Github加速代理" if github_proxy else "",
            "headers": settings.GITHUB_HEADERS,
        },
        {
            "id": "github_api",
            "name": "api.github.com",
            "icon": "github",
            "url": "https://api.github.com",
            "proxy": True,
            "allowed_redirect_prefixes": ["https://api.github.com/"],
            "headers": settings.GITHUB_HEADERS,
        },
        {
            "id": "github_codeload",
            "name": "codeload.github.com",
            "icon": "github",
            "url": "https://codeload.github.com",
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://codeload.github.com/",
                "https://github.com/",
            ],
            "headers": settings.GITHUB_HEADERS,
        },
        {
            "id": "github_proxy_raw",
            "name": "raw.githubusercontent.com",
            "icon": "github",
            "url": f"{github_proxy}{raw_readme_url}"
            if github_proxy
            else raw_readme_url,
            "proxy": True,
            "allowed_redirect_prefixes": [
                "https://raw.githubusercontent.com/",
                *(
                    (f"{github_proxy}https://raw.githubusercontent.com/",)
                    if github_proxy
                    else ()
                ),
            ],
            "expected_text": "MoviePilot",
            "invalid_message": "Github加速代理已失效，请检查配置"
            if github_proxy
            else "无效响应",
            "proxy_name": "Github加速代理" if github_proxy else "",
            "headers": settings.GITHUB_HEADERS,
        },
    ]
    if tmdb_domain not in {"api.themoviedb.org", "api.tmdb.org"}:
        rules.insert(
            2,
            {
                "id": "tmdb_api_configured",
                "name": tmdb_domain,
                "icon": "tmdb",
                "url": f"https://{tmdb_domain}/3/movie/550?api_key={tmdb_key}",
                "proxy": True,
                "allowed_redirect_prefixes": [
                    f"https://{tmdb_domain}/3/",
                ],
            },
        )
    return rules


def _collect_named_log_files(name: str) -> list[Path]:
    """
    根据前端传入的日志标识收集可下载日志文件。

    `moviepilot` 固定表示主程序日志，其余标识按插件 ID 处理并映射到
    `plugins/<plugin_id>.log*`。这里不接收路径或后缀，避免下载入口变成任意
    日志文件选择器；滚动日志按当前文件优先、备份文件按修改时间倒序补足。
    """
    normalized_name = (name or "").strip().lower()
    if not normalized_name or not _LOG_DOWNLOAD_NAME_PATTERN.fullmatch(normalized_name):
        raise HTTPException(status_code=404, detail="Not Found")

    log_root = settings.LOG_PATH
    if normalized_name == "moviepilot":
        log_dir = log_root
        log_prefix = "moviepilot.log"
    else:
        log_dir = log_root / "plugins"
        log_prefix = f"{normalized_name}.log"

    if not log_dir.exists() or not log_dir.is_dir():
        raise HTTPException(status_code=404, detail="Not Found")

    current_log = log_dir / log_prefix
    backup_logs = [
        item
        for item in log_dir.iterdir()
        if item.is_file() and item.name.startswith(f"{log_prefix}.")
    ]
    backup_logs.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    log_files = []
    if current_log.exists() and current_log.is_file():
        log_files.append(current_log)
    log_files.extend(backup_logs)
    return log_files[:_LOG_DOWNLOAD_LIMIT]


def _verify_log_resource_superuser(
    token_payload: schemas.TokenPayload = Depends(verify_resource_token),
) -> schemas.TokenPayload:
    """
    校验日志资源访问权限。

    日志接口通过浏览器新窗口和 EventSource 访问，不能依赖普通 API 请求头；
    因此这里复用资源 Cookie 完成身份识别，再额外要求管理员身份，避免普通
    登录用户读取可能包含敏感信息的日志。
    """
    if not token_payload.super_user:
        raise HTTPException(status_code=403, detail="用户权限不足")
    return token_payload


async def _build_log_zip_response(name: str) -> StreamingResponse:
    """
    将指定日志标识对应的日志文件打包为 zip 响应。

    打包前逐个校验文件仍位于日志根目录内，避免符号链接或并发文件变更绕过
    `name` 到固定目录的映射约束。zip 内使用日志根目录相对路径，便于区分
    主程序日志与插件日志。
    """
    log_files = _collect_named_log_files(name)
    if not log_files:
        raise HTTPException(status_code=404, detail="Not Found")

    log_root = settings.LOG_PATH
    async_log_root = AsyncPath(log_root)
    zip_buffer = io.BytesIO()
    filename_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = (name or "logs").strip().lower() or "logs"
    zip_stem = f"{safe_name}-logs-{filename_time}"
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for log_file in log_files:
            if not await SecurityUtils.async_is_safe_path(
                base_path=async_log_root,
                user_path=AsyncPath(log_file),
            ):
                raise HTTPException(status_code=404, detail="Not Found")
            arcname = f"{zip_stem}/{log_file.name}"
            archive.write(log_file, arcname)

    zip_buffer.seek(0)
    headers = {
        "Content-Disposition": f'attachment; filename="{zip_stem}.zip"'
    }
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers=headers,
    )


def _validate_nettest_url(url: str) -> Optional[str]:
    """
    对实际请求地址做基础安全校验。

    即使请求来自服务端内置规则，这里仍保留一层兜底校验，防止配置项被拼出
    非 HTTPS、带凭据或不在内置目标集合中的地址。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return "测试地址仅支持 HTTPS"
    if not parsed.netloc:
        return "测试地址无效"
    if parsed.username or parsed.password:
        return "测试地址不支持携带账号信息"
    if not _get_nettest_rule(url):
        return "测试地址不在允许的测试目标列表中"
    return None


def _get_nettest_rule(
    url: Optional[str] = None, target_id: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """
    根据 target_id 或历史兼容参数匹配网络测试规则。

    现在的主路径是 target_id。保留 url 参数是为了兼容旧前端或未升级的调用方，
    但匹配结果仍然只能落到服务端预定义规则上。
    """
    for rule in _build_nettest_rules():
        if target_id and rule.get("id") == target_id:
            return rule
        if url and rule.get("url") == url:
            return rule
    return None


def _is_allowed_nettest_redirect(url: str, rule: dict[str, Any]) -> bool:
    """
    校验重定向目标是否仍属于当前测试项允许的跳转范围。

    nettest 不再信任客户端跟随重定向，而是只允许在该测试项自己的白名单内跳转，
    这样既能兼容正常 30x，又不会把安全边界重新放开。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    return any(
        _match_nettest_prefix(url, prefix)
        for prefix in rule.get("allowed_redirect_prefixes", [])
    )


async def _close_nettest_response(response: Any) -> None:
    """
    安静地关闭 httpx 响应对象。

    nettest 在手动处理重定向时会提前结束部分响应读取，这里统一做资源回收，
    避免连接泄漏干扰后续测试。
    """
    if response is None or not hasattr(response, "aclose"):
        return
    try:
        await response.aclose()
    except Exception as err:
        logger.debug(f"关闭网络测试响应失败: {err}")


async def fetch_image(
    url: str,
    proxy: Optional[bool] = None,
    use_cache: bool = False,
    if_none_match: Optional[str] = None,
    cookies: Optional[str | dict] = None,
    allowed_domains: Optional[set[str]] = None,
) -> Optional[Response]:
    """
    处理图片缓存逻辑，支持HTTP缓存和磁盘缓存
    """
    if not url:
        return None

    if allowed_domains is None:
        allowed_domains = set(settings.SECURITY_IMAGE_DOMAINS)

    fetch_url = SecurityUtils.strip_url_signature(url)
    # 验证URL安全性
    if not await SecurityUtils.is_safe_image_url_async(
        url,
        allowed_domains,
        allowed_private_ranges=settings.IMAGE_PROXY_ALLOWED_PRIVATE_RANGES,
    ):
        return None

    content = await ImageHelper().async_fetch_image(
        url=fetch_url,
        proxy=proxy,
        use_cache=use_cache,
        cookies=cookies,
    )

    if content:
        # 检查 If-None-Match
        etag = HashUtils.md5(content)
        headers = RequestUtils.generate_cache_headers(etag, max_age=86400 * 7)
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        # 返回缓存图片
        return Response(
            content=content,
            media_type=UrlUtils.get_mime_type(fetch_url, "image/jpeg"),
            headers=headers,
        )
    return None


@router.get("/img/{proxy}", summary="图片代理")
async def proxy_img(
    imgurl: str,
    proxy: bool = False,
    cache: bool = False,
    use_cookies: bool = False,
    if_none_match: Annotated[str | None, Header()] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Response:
    """
    图片代理，可选是否使用代理服务器，支持 HTTP 缓存
    """
    allowed_domains = set(settings.SECURITY_IMAGE_DOMAINS)
    cookies = (
        MediaServerChain().get_image_cookies(server=None, image_url=imgurl)
        if use_cookies
        else None
    )
    return await fetch_image(
        url=imgurl,
        proxy=proxy,
        use_cache=cache,
        cookies=cookies,
        if_none_match=if_none_match,
        allowed_domains=allowed_domains,
    )


@router.get("/cache/image", summary="图片缓存")
async def cache_img(
    url: str,
    if_none_match: Annotated[str | None, Header()] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Response:
    """
    本地缓存图片文件，支持 HTTP 缓存，如果启用全局图片缓存，则使用磁盘缓存
    """
    # 如果没有启用全局图片缓存，则不使用磁盘缓存
    return await fetch_image(
        url=url, use_cache=settings.GLOBAL_IMAGE_CACHE, if_none_match=if_none_match
    )


@router.get("/global", summary="查询非敏感系统设置", response_model=schemas.Response)
def get_global_setting(token: str):
    """
    查询非敏感系统设置（默认鉴权）
    仅包含登录前UI初始化必需的字段
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")

    # 白名单模式，仅包含登录前UI初始化必需的字段
    info = settings.model_dump(
        include={
            "TMDB_IMAGE_DOMAIN",
            "GLOBAL_IMAGE_CACHE",
            "ADVANCED_MODE",
        }
    )
    # 追加版本信息（用于版本检查）
    info.update(
        {
            "FRONTEND_VERSION": SystemChain.get_frontend_version(),
            "BACKEND_VERSION": APP_VERSION,
        }
    )
    # 仅在后端开发模式下返回该标记，避免生产环境暴露无意义运行态信息
    if settings.DEV:
        info.update({"BACKEND_DEV": True})
    return schemas.Response(success=True, data=info)


@router.get(
    "/global/user", summary="查询用户相关系统设置", response_model=schemas.Response
)
async def get_user_global_setting(_: User = Depends(get_current_active_user_async)):
    """
    查询用户相关系统设置（登录后获取）
    包含业务功能相关的配置和用户权限信息
    """
    # 业务功能相关的配置字段
    info = settings.model_dump(
        include={
            "AI_AGENT_ENABLE",
            "LLM_SUPPORT_AUDIO_INPUT",
            "LLM_SUPPORT_AUDIO_OUTPUT",
            "RECOGNIZE_SOURCE",
            "SEARCH_SOURCE",
            "AI_RECOMMEND_ENABLED",
            "PASSKEY_ALLOW_REGISTER_WITHOUT_OTP",
        }
    )
    # 智能助手总开关未开启，智能推荐状态强制返回False
    if not settings.AI_AGENT_ENABLE:
        info["AI_RECOMMEND_ENABLED"] = False
        info["LLM_SUPPORT_AUDIO_INPUT"] = False
        info["LLM_SUPPORT_AUDIO_OUTPUT"] = False

    # 追加用户唯一ID和订阅分享管理权限
    share_admin = await MoviePilotServerHelper.async_is_admin_user()
    info.update(
        {
            "USER_UNIQUE_ID": MoviePilotServerHelper.get_user_uuid(),
            "SUBSCRIBE_SHARE_MANAGE": share_admin,
            "WORKFLOW_SHARE_MANAGE": share_admin,
        }
    )
    return schemas.Response(success=True, data=info)


@router.get("/env", summary="查询系统配置", response_model=schemas.Response)
async def get_env_setting(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """
    查询系统环境变量，包括当前版本号（仅管理员）
    """
    info = settings.model_dump(exclude={"SECRET_KEY", "RESOURCE_SECRET_KEY"})
    info.update(
        {
            "VERSION": APP_VERSION,
            "AUTH_VERSION": SitesHelper().auth_version,
            "INDEXER_VERSION": SitesHelper().indexer_version,
            "FRONTEND_VERSION": SystemChain().get_frontend_version(),
            "RUST_ACCEL_AVAILABLE": rust_accel.is_available(),
            "RUST_ACCEL_ENABLED": rust_accel.is_enabled(),
        }
    )
    return schemas.Response(success=True, data=info)


@router.get("/usage/statistic", summary="查询安装版本统计报表", response_model=schemas.Response)
async def usage_statistic(_: User = Depends(get_current_active_user_async)):
    """
    查询安装版本统计报表
    """
    return schemas.Response(success=True, data=await MoviePilotServerHelper.async_get_usage_statistic())


@router.get("/ping", summary="服务存活检测", response_model=schemas.Response)
async def ping(_: User = Depends(get_current_active_user_async)) -> schemas.Response:
    """
    检测服务是否可用
    """
    return schemas.Response(success=True)


@router.post("/env", summary="更新系统配置", response_model=schemas.Response)
async def set_env_setting(
    env: dict, _: User = Depends(get_current_active_superuser_async)
):
    """
    更新系统环境变量（仅管理员）
    """
    result = settings.update_settings(env=env)
    # 统计成功和失败的结果
    success_updates = {k: v for k, v in result.items() if v[0]}
    failed_updates = {k: v for k, v in result.items() if v[0] is False}

    if failed_updates:
        return schemas.Response(
            success=False,
            message=f"{', '.join([v[1] for v in failed_updates.values()])}",
            data={"success_updates": success_updates, "failed_updates": failed_updates},
        )

    if success_updates:
        # 发送配置变更事件
        await eventmanager.async_send_event(
            etype=EventType.ConfigChanged,
            data=ConfigChangeEventData(
                key=success_updates.keys(), change_type="update"
            ),
        )

    return schemas.Response(
        success=True,
        message="所有配置项更新成功",
        data={"success_updates": success_updates},
    )


@router.get("/progress/{process_type}", summary="实时进度")
async def get_progress(
    request: Request,
    process_type: str,
    _: schemas.TokenPayload = Depends(verify_resource_token),
):
    """
    实时获取处理进度，返回格式为SSE
    """
    progress = ProgressHelper(process_type)

    async def event_generator():
        try:
            while not global_vars.is_system_stopped:
                if await request.is_disconnected():
                    break
                detail = progress.get()
                yield f"data: {json.dumps(detail)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/setting/public/{key}", summary="查询公开系统设置", response_model=schemas.Response)
async def get_public_setting(
    key: str, _: User = Depends(get_current_active_user_async)
) -> schemas.Response:
    """
    查询普通用户可读取的非敏感系统设置
    """
    if key in _PUBLIC_SETTINGS_KEYS:
        return schemas.Response(success=True, data={"value": getattr(settings, key)})
    if key not in _PUBLIC_SYSTEM_CONFIG_KEYS:
        raise HTTPException(status_code=404, detail="配置项不存在")
    value = SystemConfigOper().get(_PUBLIC_SYSTEM_CONFIG_KEYS[key])
    return schemas.Response(success=True, data={"value": value})


@router.get("/setting/{key}", summary="查询系统设置", response_model=schemas.Response)
async def get_setting(
    key: str, _: User = Depends(get_current_active_superuser_async)
) -> schemas.Response:
    """
    查询系统设置（仅管理员）
    """
    if hasattr(settings, key):
        value = getattr(settings, key)
    else:
        value = SystemConfigOper().get(key)
    return schemas.Response(success=True, data={"value": value})


@router.post("/setting/{key}", summary="更新系统设置", response_model=schemas.Response)
async def set_setting(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    更新系统设置（仅管理员）
    """
    if hasattr(settings, key):
        success, message = settings.update_setting(key=key, value=value)
        if success:
            # 发送配置变更事件
            await eventmanager.async_send_event(
                etype=EventType.ConfigChanged,
                data=ConfigChangeEventData(key=key, value=value, change_type="update"),
            )
        elif success is None:
            success = True
        return schemas.Response(success=success, message=message)
    elif key in {item.value for item in SystemConfigKey}:
        if isinstance(value, list):
            value = list(filter(None, value))
            value = value if value else None
        success = await SystemConfigOper().async_set(key, value)
        if success:
            # 发送配置变更事件
            await eventmanager.async_send_event(
                etype=EventType.ConfigChanged,
                data=ConfigChangeEventData(key=key, value=value, change_type="update"),
            )
        return schemas.Response(success=True)
    else:
        return schemas.Response(success=False, message=f"配置项 '{key}' 不存在")


@router.get("/message", summary="实时消息")
async def get_message(
    request: Request,
    role: Optional[str] = "system",
    _: schemas.TokenPayload = Depends(verify_resource_token),
):
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
async def get_logging(
    request: Request,
    length: Optional[int] = 50,
    logfile: Optional[str] = "moviepilot.log",
    _: schemas.TokenPayload = Depends(_verify_log_resource_superuser),
):
    """
    实时获取系统日志
    length = -1 时, 返回text/plain
    否则 返回格式SSE
    """
    base_path = AsyncPath(settings.LOG_PATH)
    log_path = base_path / logfile

    if not await SecurityUtils.async_is_safe_path(
        base_path=base_path, user_path=log_path, allowed_suffixes={".log"}
    ):
        raise HTTPException(status_code=404, detail="Not Found")

    if not await log_path.exists() or not await log_path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")

    async def log_generator():
        try:
            # 使用固定大小的双向队列来限制内存使用
            lines_queue = deque(maxlen=max(length, 50))
            # 获取文件大小
            file_stat = await log_path.stat()
            file_size = file_stat.st_size

            # 读取历史日志
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="ignore"
            ) as f:
                # 优化大文件读取策略
                if file_size > 100 * 1024:
                    # 只读取最后100KB的内容
                    bytes_to_read = min(file_size, 100 * 1024)
                    position = file_size - bytes_to_read
                    await f.seek(position)
                    content = await f.read()
                    # 找到第一个完整的行
                    first_newline = content.find("\n")
                    if first_newline != -1:
                        content = content[first_newline + 1 :]
                else:
                    # 小文件直接读取全部内容
                    content = await f.read()

                # 按行分割并添加到队列，只保留非空行
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                # 只取最后N行
                for line in lines[-max(length, 50) :]:
                    lines_queue.append(line)

            # 输出历史日志
            for line in lines_queue:
                yield f"data: {line}\n\n"

            # 实时监听新日志
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="ignore"
            ) as f:
                # 移动文件指针到文件末尾，继续监听新增内容
                await f.seek(0, 2)
                # 记录初始文件大小
                initial_stat = await log_path.stat()
                initial_size = initial_stat.st_size
                # 实时监听新日志，使用更短的轮询间隔
                while not global_vars.is_system_stopped:
                    if await request.is_disconnected():
                        break
                    # 检查文件是否有新内容
                    current_stat = await log_path.stat()
                    current_size = current_stat.st_size
                    if current_size > initial_size:
                        # 文件有新内容，读取新行
                        line = await f.readline()
                        if line:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                        initial_size = current_size
                    else:
                        # 没有新内容，短暂等待
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as err:
            logger.error(f"日志读取异常: {err}")
            yield f"data: 日志读取异常: {err}\n\n"

    # 根据length参数返回不同的响应
    if length == -1:
        # 返回全部日志作为文本响应
        if not await log_path.exists():
            return Response(content="日志文件不存在！", media_type="text/plain")
        try:
            # 使用 aiofiles 异步读取文件
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="ignore"
            ) as file:
                text = await file.read()
            # 倒序输出
            text = "\n".join(text.split("\n")[::-1])
            return Response(content=text, media_type="text/plain")
        except Exception as e:
            return Response(content=f"读取日志文件失败: {e}", media_type="text/plain")
    else:
        # 返回SSE流响应
        return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get("/logging/download/{name}", summary="下载日志")
async def download_logging(
    name: str,
    _: schemas.TokenPayload = Depends(_verify_log_resource_superuser),
):
    """
    按日志标识下载主程序或插件滚动日志，返回 zip 文件。
    """
    return await _build_log_zip_response(name)


@router.get(
    "/versions", summary="查询Github所有Release版本", response_model=schemas.Response
)
async def latest_version(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询Github所有Release版本
    """
    version_res = await AsyncRequestUtils(
        proxies=settings.PROXY, headers=settings.GITHUB_HEADERS
    ).get_res(f"https://api.github.com/repos/jxxghp/MoviePilot/releases")
    if version_res is not None and version_res.status_code == 200:
        ver_json = version_res.json()
        if ver_json:
            return schemas.Response(success=True, data=ver_json)
    return schemas.Response(success=False)


@router.get("/ruletest", summary="过滤规则测试", response_model=schemas.Response)
def ruletest(
    title: str,
    rulegroup_name: str,
    subtitle: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
):
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
        return schemas.Response(
            success=False, message=f"过滤规则组 {rulegroup_name} 不存在！"
        )

    # 根据标题查询媒体信息
    media_info = MediaChain().recognize_by_meta(
        MetaInfo(title=title, subtitle=subtitle),
        obtain_images=False,
    )
    if not media_info:
        return schemas.Response(success=False, message="未识别到媒体信息！")

    # 过滤
    result = SearchChain().filter_torrents(
        rule_groups=[rulegroup.name], torrent_list=[torrent], mediainfo=media_info
    )
    if not result:
        return schemas.Response(success=False, message="不符合过滤规则！")
    return schemas.Response(
        success=True, data={"priority": 100 - result[0].pri_order + 1}
    )


@router.get(
    "/nettest/targets", summary="获取网络测试目标", response_model=schemas.Response
)
async def nettest_targets(_: schemas.TokenPayload = Depends(verify_token)):
    """
    获取网络测试目标。

    这里只返回前端渲染所需的最小信息，避免把可请求 URL、内容校验规则和
    跳转白名单暴露给客户端。
    """
    return schemas.Response(
        success=True,
        data=[
            {
                "id": item["id"],
                "name": item["name"],
                "icon": item["icon"],
            }
            for item in _build_nettest_rules()
        ],
    )


@router.get("/nettest", summary="测试网络连通性")
async def nettest(
    target_id: Optional[str] = None,
    url: Optional[str] = None,
    include: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
):
    """
    测试内置目标的网络连通性。

    `target_id` 是当前前端使用的正式入口。`url/proxy/include` 仅作兼容保留，
    其中 `include` 不再参与客户端可控的内容匹配，具体校验由服务端规则决定。
    """
    target = _get_nettest_rule(url=url, target_id=target_id)
    if not target:
        return schemas.Response(success=False, message="测试目标不存在")
    # 记录开始的毫秒数
    start_time = datetime.now()
    url = target["url"]
    invalid_message = _validate_nettest_url(url)
    if invalid_message:
        logger.warning(f"拦截不安全的网络测试地址: {url}")
        return schemas.Response(success=False, message=invalid_message)
    if include:
        logger.debug("nettest include 参数已忽略，改为服务端固定校验")

    request_utils = AsyncRequestUtils(
        proxies=settings.PROXY if target.get("proxy") else None,
        headers=target.get("headers"),
        timeout=10,
        ua=settings.NORMAL_USER_AGENT,
        verify=True,
        follow_redirects=False,
    )
    result = None
    current_url = url
    redirect_count = 0
    while redirect_count <= 3:
        result = await request_utils.get_res(current_url, allow_redirects=False)
        if result is None:
            break
        if result.status_code not in _NETTEST_REDIRECT_STATUS_CODES:
            break
        location = result.headers.get("location")
        if not location:
            break
        next_url = urljoin(current_url, location)
        if not _is_allowed_nettest_redirect(next_url, target):
            await _close_nettest_response(result)
            logger.warning(f"拦截网络测试重定向: {current_url} -> {next_url}")
            return schemas.Response(success=False, message="测试目标发生了未授权跳转")
        await _close_nettest_response(result)
        current_url = next_url
        redirect_count += 1
    if redirect_count > 3:
        return schemas.Response(success=False, message="测试目标重定向次数过多")
    # 计时结束的毫秒数
    end_time = datetime.now()
    time = round((end_time - start_time).total_seconds() * 1000)
    # 计算相关秒数
    if result is None:
        return schemas.Response(
            success=False,
            message=f"{target.get('proxy_name') or target.get('name')}无法连接",
            data={"time": time},
        )
    elif result.status_code == 200:
        expected_text = target.get("expected_text")
        if expected_text and expected_text.lower() not in (result.text or "").lower():
            return schemas.Response(
                success=False,
                message=target.get("invalid_message") or "无效响应",
                data={"time": time},
            )
        return schemas.Response(success=True, data={"time": time})
    else:
        if target.get("proxy_name"):
            # 加速代理失败
            message = f"{target['proxy_name']}已失效，错误码：{result.status_code}"
        else:
            message = f"错误码：{result.status_code}"
            if "github" in url:
                # 非加速代理访问github
                if result.status_code == 401:
                    message = "Github Token已失效，请检查配置"
                elif result.status_code in {403, 429}:
                    message = "触发限流，请配置Github Token"
        return schemas.Response(success=False, message=message, data={"time": time})


@router.get(
    "/modulelist", summary="查询已加载的模块ID列表", response_model=schemas.Response
)
def modulelist(_: schemas.TokenPayload = Depends(verify_token)):
    """
    查询已加载的模块ID列表
    """
    modules = [
        {
            "id": k,
            "name": v.get_name(),
        }
        for k, v in ModuleManager().get_modules().items()
    ]
    return schemas.Response(success=True, data={"modules": modules})


@router.get(
    "/moduletest/{moduleid}", summary="模块可用性测试", response_model=schemas.Response
)
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
    if not ret:
        global_vars.resume_system()
    return schemas.Response(success=ret, message=msg)


@router.post("/upgrade", summary="升级并重启系统", response_model=schemas.Response)
def upgrade_system(
    mode: Annotated[str | None, Body()] = None,
    _: User = Depends(get_current_active_superuser),
):
    """
    触发系统升级并重启（仅管理员）

    - 当前已开启自动升级时：直接重启，由启动流程完成升级。
    - 当前未开启自动升级时：写入一次性升级标记，本次重启后仅执行一次升级。
    """
    if not SystemHelper.can_restart():
        return schemas.Response(success=False, message="当前运行环境不支持升级操作！")

    # 标识停止事件
    global_vars.stop_system()
    ret, msg = SystemHelper.upgrade(mode=mode or "release")
    if not ret:
        global_vars.resume_system()
    return schemas.Response(success=ret, message=msg)


@router.get("/runscheduler", summary="运行服务", response_model=schemas.Response)
def run_scheduler(jobid: str, _: User = Depends(get_current_active_superuser)):
    """
    执行命令（仅管理员）
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")
    if jobid in {"recommend_refresh", "cookiecloud"}:
        Scheduler().start(jobid, manual=True)
    else:
        Scheduler().start(jobid)
    return schemas.Response(success=True)


@router.get(
    "/runscheduler2", summary="运行服务（API_TOKEN）", response_model=schemas.Response
)
def run_scheduler2(jobid: str, _: Annotated[str, Depends(verify_apitoken)]):
    """
    执行命令（API_TOKEN认证）
    """
    if not jobid:
        return schemas.Response(success=False, message="命令不能为空！")

    if jobid in {"recommend_refresh", "cookiecloud"}:
        Scheduler().start(jobid, manual=True)
    else:
        Scheduler().start(jobid)
    return schemas.Response(success=True)
