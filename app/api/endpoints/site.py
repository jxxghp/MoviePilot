from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import Depends, HTTPException, Response

from app.adapters.web.security.access import verify_token
from app.api.context import get_background_task_registry, resolve_background_task_registry
from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.dependencies.site import (
    get_site_mutation_command,
    get_site_query_service,
    get_site_sync_query_service,
)
from app.api.endpoints.plugin import register_plugin_api
from app.api.principal import ApiPrincipal
from app.api.response import (
    COLLECTION_TOTAL_HEADER,
    COLLECTION_TOTAL_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
    resolve_compatible_pagination,
)
from app.application.commands import init_commands
from app.application.configuration import get_configured_system_config
from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import get_scheduler
from app.application.site.mutation import SiteMutationCommand
from app.application.site.query import SiteQueryService
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.domain import site as site_rules
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry
from app.schemas.common import JsonData
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.response import Response as _SchemaResponse
from app.schemas.site import SiteAuth as _SchemaSiteAuth
from app.schemas.site import SiteCategory as _SchemaSiteCategory
from app.schemas.site import SiteCookieUpdate as _SchemaSiteCookieUpdate
from app.schemas.site import SiteIconData as _SchemaSiteIconData
from app.schemas.site import SiteMappingData as _SchemaSiteMappingData
from app.schemas.site import SitePriorityUpdate as _SchemaSitePriorityUpdate
from app.schemas.site import SiteStatistic as _SchemaSiteStatistic
from app.schemas.site import SiteUserData as _SchemaSiteUserData
from app.schemas.system import TorrentInfo as _SchemaTorrentInfo
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import MediaType, SystemConfigKey
from app.schemas.workflow import Site as _SchemaSite

router = ResponseAPIRouter()


def _project_agent_site(site: Any, *, include_secrets: bool) -> dict[str, JsonData]:
    """构造旧 Agent 查询语义的站点安全投影，普通用户不返回认证凭据。"""
    projected: dict[str, JsonData] = {
        "id": site.id,
        "name": site.name,
        "domain": site.domain,
        "url": site.url,
        "pri": site.pri,
        "is_active": site.is_active,
        "downloader": site.downloader,
        "ua": site.ua,
        "proxy": site.proxy,
        "filter": site.filter,
        "render": site.render,
        "public": site.public,
        "note": site.note,
        "limit_interval": site.limit_interval,
        "limit_count": site.limit_count,
        "limit_seconds": site.limit_seconds,
        "timeout": site.timeout,
    }
    if include_secrets:
        projected.update(
            {
                "rss": site.rss,
                "cookie": site.cookie,
                "apikey": site.apikey,
                "token": site.token,
            }
        )
    return projected


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


def _normalize_site_ids(values: Any) -> list[int]:
    """把配置或索引器中的站点标识归一为可查询的整数主键。"""
    normalized: list[int] = []
    for value in values or []:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(normalized))


@router.get(
    "/",
    summary="所有站点",
    response_model=List[_SchemaSite],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_sites(
    response: Response = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[dict]:
    """
    获取站点列表
    """
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_ordered()
        )
    return await query.list_ordered(page=page, count=count)


@router.get(  # type: ignore[misc]
    "/agent",
    summary="查询 Agent 可用站点",
    response_model=List[_SchemaJsonObject],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_agent_sites(
    response: Response = None,
    status: Literal["active", "inactive", "all"] = "all",
    name: Optional[str] = None,
    query: SiteQueryService = Depends(get_site_query_service),
    current_user: Any = Depends(get_current_active_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[dict[str, JsonData]]:
    """按旧 Agent 过滤语义返回站点，非超级管理员自动剔除认证字段。"""
    active_filter = None
    if status == "active":
        active_filter = True
    elif status == "inactive":
        active_filter = False
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_ordered(is_active=active_filter, name=name)
        )
    sites = await query.list_ordered(
        is_active=active_filter,
        name=name,
        page=page,
        count=count,
    )
    results = []
    for site in sites:
        results.append(
            _project_agent_site(
                site,
                include_secrets=bool(current_user.is_superuser),
            )
        )
    return results


@router.get(
    "/media/{media_type}",
    summary="按媒体类型获取可搜索站点",
    response_model=List[_SchemaSite],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_sites_by_media_type(
    media_type: str,
    response: Response = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[_SchemaSite]:
    """
    获取支持指定媒体类型的已配置启用站点。

    :param media_type: Agent 媒体类型名称或中文媒体类型
    :param query: 站点查询服务
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

    supported_ids: set[int] = set()
    supported_domains: set[str] = set()
    for indexer in await SitesHelper().async_get_indexers() or []:
        if not _indexer_supports_media_type(indexer, target_media_type):
            continue
        if indexer.get("id") is not None:
            supported_ids.update(_normalize_site_ids([indexer.get("id")]))
        domain = site_rules.extract_domain(indexer.get("domain"))
        if domain:
            supported_domains.add(domain)

    site_ids = sorted(supported_ids)
    domains = sorted(supported_domains)
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_ordered(
                is_active=True,
                site_ids=site_ids,
                domains=domains,
            )
        )
    return await query.list_ordered(
        is_active=True,
        site_ids=site_ids,
        domains=domains,
        page=page,
        count=count,
    )


@router.post("/", summary="新增站点", response_model=_SchemaResponse[None])
async def add_site(
    *,
    site_in: _SchemaSite,
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
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
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    更新站点信息
    """
    result = await command.update(site_in.model_dump())
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(
    "/cookiecloud",
    summary="CookieCloud同步（兼容入口）",
    response_model=_SchemaResponse[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/cookiecloud",
    summary="CookieCloud同步",
    response_model=_SchemaResponse[None],
)
async def cookie_cloud_sync(
    task_registry: Annotated[TaskRegistry, Depends(get_background_task_registry)],
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    运行CookieCloud同步站点信息
    """
    resolve_background_task_registry(task_registry).create_sync(
        get_scheduler().start, job_id="cookiecloud", owner="api.site.cookiecloud_sync"
    )
    return _SchemaResponse(success=True, message="CookieCloud同步任务已启动！")


@router.get(
    "/reset",
    summary="重置站点（兼容入口）",
    response_model=_SchemaResponse[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/reset",
    summary="重置站点",
    response_model=_SchemaResponse[None],
)
async def reset(
    task_registry: Annotated[TaskRegistry, Depends(get_background_task_registry)],
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    清空所有站点数据并重新同步CookieCloud站点信息
    """
    result = await command.reset()
    await get_configured_system_config().async_set(SystemConfigKey.IndexerSites, [])
    await get_configured_system_config().async_set(SystemConfigKey.RssSites, [])
    resolve_background_task_registry(task_registry).create_sync(
        get_scheduler().start,
        job_id="cookiecloud",
        owner="api.site.reset",
        manual=True,
    )
    return _SchemaResponse(success=result.success, message="站点已重置！")


@router.post(
    "/priorities", summary="批量更新站点优先级", response_model=_SchemaResponse[None]
)
async def update_sites_priority(
    priorities: List[_SchemaSitePriorityUpdate],
    command: SiteMutationCommand = Depends(get_site_mutation_command),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    批量更新站点优先级
    """
    result = await command.update_priorities([priority.model_dump() for priority in priorities])
    return _SchemaResponse(success=result.success, message=result.message)


def _update_site_cookie(
    site_id: int,
    username: str,
    password: str,
    code: Optional[str],
    query: SiteQueryService,
) -> _SchemaResponse:
    """
    执行站点 Cookie 与 UA 更新。

    :param site_id: 站点编号
    :param username: 站点登录用户名
    :param password: 站点登录密码
    :param code: 二步验证码或密钥
    :param query: 站点查询服务
    :return: 更新结果
    """
    site_info = query.get_sync(site_id)
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
    query: SiteQueryService = Depends(get_site_sync_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    使用请求体中的用户密码更新站点Cookie
    """
    return _update_site_cookie(
        site_id=site_id,
        username=site_cookie_update.username,
        password=site_cookie_update.password,
        code=site_cookie_update.code,
        query=query,
    )


@router.get(
    "/cookie/{site_id}", summary="更新站点Cookie&UA", response_model=_SchemaResponse[None]
)
def update_cookie(
    site_id: int,
    username: str,
    password: str,
    code: Optional[str] = None,
    query: SiteQueryService = Depends(get_site_sync_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    使用用户密码更新站点Cookie
    """
    return _update_site_cookie(
        site_id=site_id,
        username=username,
        password=password,
        code=code,
        query=query,
    )


@router.post(
    "/userdata/{site_id}",
    summary="更新站点用户数据",
    response_model=_SchemaResponse[_SchemaSiteUserData],
)
def refresh_userdata(
    site_id: int,
    query: SiteQueryService = Depends(get_site_sync_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    刷新站点用户数据
    """
    site = query.get_sync(site_id)
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
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_userdata_latest(
    response: Response = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询所有站点最新用户数据
    """
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_userdata_latest()
        )
    return await query.userdata_latest(page=page, count=count)


@router.get(
    "/userdata/{site_id}",
    summary="查询某站点用户数据",
    response_model=_SchemaResponse[list[_SchemaSiteUserData]],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_userdata(
    site_id: int,
    response: Response = None,
    workdate: Optional[str] = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询站点用户数据
    """
    site = await query.get(site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    if not site.domain:
        raise HTTPException(
            status_code=409,
            detail=f"站点 {site_id} 未配置域名",
        )
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_userdata(site.domain, workdate)
        )
    user_datas = await query.userdata(
        site.domain,
        workdate,
        page=page,
        count=count,
    )
    if not user_datas:
        return _SchemaResponse(success=False, data=[])
    return _SchemaResponse(success=True, data=user_datas)


@router.get("/test/{site_id}", summary="连接测试", response_model=_SchemaResponse[None])
def test_site(
    site_id: int,
    query: SiteQueryService = Depends(get_site_sync_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    测试站点是否可用
    """
    site = query.get_sync(site_id)
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
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    获取站点图标：base64或者url
    """
    site = await query.get(site_id)
    if not site:
        raise HTTPException(
            status_code=404,
            detail=f"站点 {site_id} 不存在",
        )
    icon = await query.icon(site.domain)
    if not icon:
        return _SchemaResponse(success=False, message="站点图标不存在！")
    return _SchemaResponse(success=True, data=icon.model_dump())


@router.get(
    "/category/{site_id}", summary="站点分类", response_model=List[_SchemaSiteCategory]
)
async def site_category(
    site_id: int,
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    获取站点分类
    """
    site = await query.get(site_id)
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
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    浏览站点资源
    """
    site = await query.get(site_id)
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
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    通过域名获取站点信息
    """
    domain = site_rules.extract_domain(site_url)
    site = await query.get_by_domain(domain)
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
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    通过域名获取站点统计信息
    """
    domain = site_rules.extract_domain(site_url)
    return await query.statistic(domain)


@router.get(
    "/statistic",
    summary="所有站点统计信息",
    response_model=List[_SchemaSiteStatistic],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_statistics(
    response: Response = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    获取所有站点统计信息
    """
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_statistics()
        )
    return await query.statistics(page=page, count=count)


@router.get(
    "/rss",
    summary="所有订阅站点",
    response_model=List[_SchemaSite],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_rss_sites(
    response: Response = None,
    query: SiteQueryService = Depends(get_site_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[dict]:
    """
    获取站点列表
    """
    # 选中的rss站点
    selected_sites = get_configured_system_config().get(SystemConfigKey.RssSites) or []

    site_ids = _normalize_site_ids(selected_sites) if selected_sites else None
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_ordered(site_ids=site_ids)
        )
    return await query.list_ordered(
        site_ids=site_ids,
        page=page,
        count=count,
    )


@router.get("/auth", summary="查询认证站点", response_model=_SchemaJsonObject)
async def read_auth_sites(_: _SchemaTokenPayload = Depends(verify_token)) -> dict:
    """
    获取可认证站点列表
    """
    return SitesHelper().get_authsites()


@router.post("/auth", summary="用户站点认证", response_model=_SchemaResponse[None])
def auth_site(
    auth_info: _SchemaSiteAuth, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> Any:
    """
    用户站点认证
    """
    if not auth_info or not auth_info.site or not auth_info.params:
        return _SchemaResponse(success=False, message="请输入认证站点和认证参数")
    status, msg = SitesHelper().check_user(auth_info.site, auth_info.params)
    get_configured_system_config().set(SystemConfigKey.UserSiteAuthParams, auth_info.model_dump())
    # 认证成功后，重新初始化插件
    get_plugin_manager().init_config()
    get_scheduler().init_plugin_jobs()
    init_commands()
    register_plugin_api()
    return _SchemaResponse(success=status, message=msg)


@router.get(
    "/mapping",
    summary="获取站点域名到名称的映射",
    response_model=_SchemaResponse[_SchemaSiteMappingData],
)
async def site_mapping(
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
):
    """
    获取站点域名到名称的映射关系
    """
    try:
        sites = await query.list_ordered()
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
async def support_sites(_: ApiPrincipal = Depends(get_current_active_superuser_async)):
    """
    获取支持的站点列表
    """
    return SitesHelper().get_indexsites()


@router.get("/{site_id}", summary="站点详情", response_model=_SchemaSite)
async def read_site(
    site_id: int,
    query: SiteQueryService = Depends(get_site_query_service),
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    通过ID获取站点信息
    """
    site = await query.get(site_id)
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
    _: ApiPrincipal = Depends(get_current_active_manage_user_async),
) -> Any:
    """
    删除站点
    """
    result = await command.delete(site_id)
    return _SchemaResponse(success=result.success, message=result.message)
