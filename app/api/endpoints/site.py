from typing import List, Any, Dict, Optional

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.background import BackgroundTasks

from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.response import Response as _SchemaResponse
from app.schemas.site import SiteAuth as _SchemaSiteAuth
from app.schemas.site import SiteCategory as _SchemaSiteCategory
from app.schemas.site import SiteCookieUpdate as _SchemaSiteCookieUpdate
from app.schemas.site import SiteIconData as _SchemaSiteIconData
from app.schemas.site import SiteMappingData as _SchemaSiteMappingData
from app.schemas.site import SiteStatistic as _SchemaSiteStatistic
from app.schemas.site import SiteUserData as _SchemaSiteUserData
from app.schemas.system import TorrentInfo as _SchemaTorrentInfo
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.workflow import Site as _SchemaSite
from app.api.response import ResponseAPIRouter
from app.application.site.mutation import SiteMutationCommand
from app.api.endpoints.plugin import register_plugin_api
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.command import Command
from app.runtime.events import eventmanager
from app.runtime.extensions.plugin_manager import PluginManager
from app.application.security.access import verify_token
from app.db import get_db, get_async_db
from app.db.models import User
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.site import SiteOper
from app.db.oper.systemconfig import SystemConfigOper
from app.api.deps import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_site_mutation_command,
)
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.runtime.log import logger
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey, EventType, MediaType
from app.domain import site as site_rules

router = ResponseAPIRouter()


def _indexer_supports_media_type(indexer: dict, media_type: MediaType) -> bool:
    """
    判断站点索引器是否支持指定媒体类型。

    :param indexer: 站点索引器配置
    :param media_type: 待搜索的媒体类型
    :return: 是否应在该媒体类型的站点选择列表中显示
    """
    declared_media_type = indexer.get("media_type")
    if isinstance(declared_media_type, MediaType):
        site_media_type = declared_media_type
    elif isinstance(declared_media_type, str):
        site_media_type = MediaType.from_agent(declared_media_type)
    else:
        site_media_type = None
    if site_media_type:
        return site_media_type == media_type

    categories = indexer.get("category") or {}
    if not isinstance(categories, dict):
        return media_type != MediaType.MUSIC

    category_key = media_type.to_agent()
    if media_type == MediaType.MUSIC:
        return bool(categories.get(category_key))

    declared_category_keys = {
        item.to_agent()
        for item in (MediaType.MOVIE, MediaType.TV, MediaType.MUSIC)
        if categories.get(item.to_agent())
    }
    if declared_category_keys:
        return category_key in declared_category_keys
    return True


@router.get("/", summary="所有站点", response_model=List[_SchemaSite])
async def read_sites(
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> List[dict]:
    """
    获取站点列表
    """
    return await Site.async_list_order_by_pri(db)


@router.get(
    "/media/{media_type}",
    summary="按媒体类型获取可搜索站点",
    response_model=List[_SchemaSite],
)
async def read_sites_by_media_type(
    media_type: str,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> List[Site]:
    """
    获取支持指定媒体类型的已配置启用站点。

    :param media_type: Agent 媒体类型名称或中文媒体类型
    :param db: 异步数据库会话
    :return: 按优先级排序的可搜索站点
    """
    target_media_type = MediaType.from_agent(media_type)
    if not target_media_type:
        try:
            target_media_type = MediaType(media_type)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="不支持的媒体类型") from error
    if target_media_type not in (MediaType.MOVIE, MediaType.TV, MediaType.MUSIC):
        raise HTTPException(status_code=400, detail="不支持的媒体类型")

    supported_ids = set()
    supported_domains = set()
    for indexer in await SitesHelper().async_get_indexers() or []:
        if not _indexer_supports_media_type(indexer, target_media_type):
            continue
        if indexer.get("id") is not None:
            supported_ids.add(str(indexer.get("id")))
        domain = site_rules.extract_domain(indexer.get("domain"))
        if domain:
            supported_domains.add(domain)

    sites = await Site.async_list_order_by_pri(db)
    return [
        site
        for site in sites
        if site.is_active
        and (str(site.id) in supported_ids or site.domain in supported_domains)
    ]


@router.post("/", summary="新增站点", response_model=_SchemaResponse[None])
async def add_site(
    *,
    site_in: _SchemaSite,
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    新增站点
    """
    result = await command.create(site_in.model_dump())
    return _SchemaResponse(success=result.success, message=result.message)


@router.put("/", summary="更新站点", response_model=_SchemaResponse[None])
async def update_site(
    *,
    site_in: _SchemaSite,
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    更新站点信息
    """
    result = await command.update(site_in.model_dump())
    return _SchemaResponse(success=result.success, message=result.message)


@router.get("/cookiecloud", summary="CookieCloud同步", response_model=_SchemaResponse[None])
async def cookie_cloud_sync(
    background_tasks: BackgroundTasks,
    _: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    运行CookieCloud同步站点信息
    """
    background_tasks.add_task(Scheduler().start, job_id="cookiecloud")
    return _SchemaResponse(success=True, message="CookieCloud同步任务已启动！")


@router.get("/reset", summary="重置站点", response_model=_SchemaResponse[None])
def reset(
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    清空所有站点数据并重新同步CookieCloud站点信息
    """
    Site.reset(db)
    SystemConfigOper().set(SystemConfigKey.IndexerSites, [])
    SystemConfigOper().set(SystemConfigKey.RssSites, [])
    # 启动定时服务
    Scheduler().start("cookiecloud", manual=True)
    # 插件站点删除
    eventmanager.send_event(EventType.SiteDeleted, {"site_id": "*"})
    return _SchemaResponse(success=True, message="站点已重置！")


@router.post(
    "/priorities", summary="批量更新站点优先级", response_model=_SchemaResponse[None]
)
async def update_sites_priority(
    priorities: List[dict],
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    批量更新站点优先级
    """
    result = await command.update_priorities(priorities)
    return _SchemaResponse(success=result.success, message=result.message)


def _update_site_cookie(
    site_id: int,
    username: str,
    password: str,
    code: Optional[str],
    db: Session,
) -> _SchemaResponse:
    """
    执行站点 Cookie 与 UA 更新。

    :param site_id: 站点编号
    :param username: 站点登录用户名
    :param password: 站点登录密码
    :param code: 二步验证码或密钥
    :param db: 数据库会话
    :return: 更新结果
    """
    site_info = Site.get(db, site_id)
    if not site_info:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在！",
        )
    logger.info(f"开始更新站点【{site_info.name}】Cookie&UA")
    state, message = SiteChain().update_cookie(
        site_info=site_info, username=username, password=password, two_step_code=code
    )
    if state:
        logger.info(f"站点【{site_info.name}】Cookie&UA更新成功")
    else:
        logger.error(f"站点【{site_info.name}】Cookie&UA更新失败：{message}")
    return _SchemaResponse(success=state, message=message)


@router.post(
    "/cookie/{site_id}", summary="更新站点Cookie&UA", response_model=_SchemaResponse[None]
)
def update_cookie_by_body(
    site_id: int,
    site_cookie_update: _SchemaSiteCookieUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    使用请求体中的用户密码更新站点Cookie
    """
    return _update_site_cookie(
        site_id=site_id,
        username=site_cookie_update.username,
        password=site_cookie_update.password,
        code=site_cookie_update.code,
        db=db,
    )


@router.get(
    "/cookie/{site_id}", summary="更新站点Cookie&UA", response_model=_SchemaResponse[None]
)
def update_cookie(
    site_id: int,
    username: str,
    password: str,
    code: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    使用用户密码更新站点Cookie
    """
    return _update_site_cookie(
        site_id=site_id,
        username=username,
        password=password,
        code=code,
        db=db,
    )


@router.post(
    "/userdata/{site_id}",
    summary="更新站点用户数据",
    response_model=_SchemaResponse[_SchemaSiteUserData],
)
def refresh_userdata(
    site_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    刷新站点用户数据
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    indexer = SitesHelper().get_indexer(site.domain)
    if not indexer:
        return _SchemaResponse(
            success=False, message="站点不支持索引或未通过用户认证！"
        )
    user_data = SiteChain().refresh_userdata(site=indexer) or {}
    return _SchemaResponse(success=True, data=user_data)


@router.get(
    "/userdata/latest",
    summary="查询所有站点最新用户数据",
    response_model=List[_SchemaSiteUserData],
)
async def read_userdata_latest(
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    查询所有站点最新用户数据
    """
    user_datas = await SiteUserData.async_get_latest(db)
    if not user_datas:
        return []
    return [user_data.to_dict() for user_data in user_datas]


@router.get(
    "/userdata/{site_id}",
    summary="查询某站点用户数据",
    response_model=_SchemaResponse[list[_SchemaSiteUserData]],
)
async def read_userdata(
    site_id: int,
    workdate: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    查询站点用户数据
    """
    site = await Site.async_get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    user_datas = await SiteUserData.async_get_by_domain(
        db, domain=site.domain, workdate=workdate
    )
    if not user_datas:
        return _SchemaResponse(success=False, data=[])
    return _SchemaResponse(success=True, data=[data.to_dict() for data in user_datas])


@router.get("/test/{site_id}", summary="连接测试", response_model=_SchemaResponse[None])
def test_site(
    site_id: int,
    db: Session = Depends(get_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    测试站点是否可用
    """
    site = Site.get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    status, message = SiteChain().test(site.domain)
    return _SchemaResponse(success=status, message=message)


@router.get(
    "/icon/{site_id}",
    summary="站点图标",
    response_model=_SchemaResponse[_SchemaSiteIconData],
)
async def site_icon(
    site_id: int,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取站点图标：base64或者url
    """
    site = await Site.async_get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    icon = await SiteIcon.async_get_by_domain(db, site.domain)
    if not icon:
        return _SchemaResponse(success=False, message="站点图标不存在！")
    return _SchemaResponse(
        success=True, data={"icon": icon.base64 if icon.base64 else icon.url}
    )


@router.get(
    "/category/{site_id}", summary="站点分类", response_model=List[_SchemaSiteCategory]
)
async def site_category(
    site_id: int,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取站点分类
    """
    site = await Site.async_get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    indexer = await SitesHelper().async_get_indexer(site.domain)
    if not indexer:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site.domain} 不支持",
        )
    category: Dict[str, List[dict]] = indexer.get("category") or []
    if not category:
        return []
    result = []
    for cats in category.values():
        for cat in cats:
            if cat not in result:
                result.append(cat)
    return result


@router.get(
    "/resource/{site_id}", summary="站点资源", response_model=List[_SchemaTorrentInfo]
)
async def site_resource(
    site_id: int,
    keyword: Optional[str] = None,
    mtype: Optional[str] = None,
    cat: Optional[str] = None,
    page: Optional[int] = 0,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    浏览站点资源
    """
    site = await Site.async_get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    torrents = await TorrentsChain().async_browse(
        domain=site.domain,
        keyword=keyword,
        cat=cat,
        page=page,
        mtype=MediaType.from_agent(mtype) or MediaType(mtype) if mtype else None,
    )
    if not torrents:
        return []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/domain/{site_url}", summary="站点详情", response_model=_SchemaSite)
async def read_site_by_domain(
    site_url: str,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    通过域名获取站点信息
    """
    domain = site_rules.extract_domain(site_url)
    site = await Site.async_get_by_domain(db, domain)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {domain} 不存在",
        )
    return site


@router.get(
    "/statistic/{site_url}",
    summary="特定站点统计信息",
    response_model=_SchemaSiteStatistic,
)
async def read_statistic_by_domain(
    site_url: str,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    通过域名获取站点统计信息
    """
    domain = site_rules.extract_domain(site_url)
    sitestatistic = await SiteStatistic.async_get_by_domain(db, domain)
    if sitestatistic:
        return sitestatistic
    return _SchemaSiteStatistic(domain=domain)


@router.get(
    "/statistic", summary="所有站点统计信息", response_model=List[_SchemaSiteStatistic]
)
async def read_statistics(
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取所有站点统计信息
    """
    return await SiteStatistic.async_list(db)


@router.get("/rss", summary="所有订阅站点", response_model=List[_SchemaSite])
async def read_rss_sites(
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> List[dict]:
    """
    获取站点列表
    """
    # 选中的rss站点
    selected_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []

    # 所有站点
    all_site = await Site.async_list_order_by_pri(db)
    if not selected_sites:
        return all_site

    # 选中的rss站点
    rss_sites = [site for site in all_site if site and site.id in selected_sites]
    return rss_sites


@router.get("/auth", summary="查询认证站点", response_model=_SchemaJsonObject)
async def read_auth_sites(_: _SchemaTokenPayload = Depends(verify_token)) -> dict:
    """
    获取可认证站点列表
    """
    return SitesHelper().get_authsites()


@router.post("/auth", summary="用户站点认证", response_model=_SchemaResponse[None])
def auth_site(
    auth_info: _SchemaSiteAuth, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    用户站点认证
    """
    if not auth_info or not auth_info.site or not auth_info.params:
        return _SchemaResponse(success=False, message="请输入认证站点和认证参数")
    status, msg = SitesHelper().check_user(auth_info.site, auth_info.params)
    SystemConfigOper().set(SystemConfigKey.UserSiteAuthParams, auth_info.model_dump())
    # 认证成功后，重新初始化插件
    PluginManager().init_config()
    Scheduler().init_plugin_jobs()
    Command().init_commands()
    register_plugin_api()
    return _SchemaResponse(success=status, message=msg)


@router.get(
    "/mapping",
    summary="获取站点域名到名称的映射",
    response_model=_SchemaResponse[_SchemaSiteMappingData],
)
async def site_mapping(_: User = Depends(get_current_active_superuser_async)):
    """
    获取站点域名到名称的映射关系
    """
    try:
        sites = await SiteOper().async_list()
        mapping = {}
        for site in sites:
            mapping[site.domain] = site.name
        return _SchemaResponse(success=True, data=mapping)
    except Exception as e:
        return _SchemaResponse(success=False, message=f"获取映射失败：{str(e)}")


@router.get(
    "/supporting",
    summary="获取支持的站点列表",
    response_model=_SchemaJsonObject,
)
async def support_sites(_: User = Depends(get_current_active_superuser_async)):
    """
    获取支持的站点列表
    """
    return SitesHelper().get_indexsites()


@router.get("/{site_id}", summary="站点详情", response_model=_SchemaSite)
async def read_site(
    site_id: int,
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    通过ID获取站点信息
    """
    site = await Site.async_get(db, site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    return site


@router.delete("/{site_id}", summary="删除站点", response_model=_SchemaResponse[None])
async def delete_site(
    site_id: int,
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: User = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    删除站点
    """
    result = await command.delete(site_id)
    return _SchemaResponse(success=result.success, message=result.message)
