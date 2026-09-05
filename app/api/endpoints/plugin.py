import asyncio
import mimetypes
from typing import Annotated, Any, Dict, List, Optional

import aiofiles
from anyio import Path as AsyncPath
from fastapi import Depends, Header, HTTPException, Query, Response, Security
from starlette import status
from starlette.responses import StreamingResponse

from app.adapters.web.security.access import (
    resource_token_cookie,
    verify_resource_token,
    verify_token,
)
from app.api.context import (
    get_background_task_registry,
    get_host_runtime,
    resolve_background_task_registry,
)
from app.api.dependencies.auth import (
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.api.dependencies.plugin import get_plugin_config_command
from app.api.endpoints.pluginfolder import router as plugin_folders_router
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
from app.application.configuration import get_api_runtime_config_snapshot, get_configured_system_config
from app.application.plugin.catalog import get_plugin_catalog_query
from app.application.plugin.config import PluginConfigCommand
from app.application.plugin.data import PluginDataQueryService, PluginDataSummaryService
from app.application.plugin.folders import add_clone_to_plugin_folder, remove_plugin_from_folders
from app.application.plugin.gateway import get_plugin_install_service
from app.application.plugin.management import (
    get_plugin_snapshot,
    reload_plugin_runtime,
    search_plugin_candidates,
)
from app.application.plugin.rating import PluginNotInstalledError, get_plugin_rating_service
from app.application.plugin.release import get_plugin_release_service
from app.application.plugin.routes import register_plugin_api, remove_plugin_api
from app.application.plugin.runtime import get_plugin_manager
from app.application.plugin.transaction import get_plugin_persistence
from app.application.scheduling import remove_plugin_job, update_plugin_job
from app.runtime.extensions.plugin.contracts import PluginDashboardError, PluginNotFoundError
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import Plugin as _SchemaPlugin
from app.schemas.plugin import PluginCloneRequest as _SchemaPluginCloneRequest
from app.schemas.plugin import PluginDashboard as _SchemaPluginDashboard
from app.schemas.plugin import PluginDashboardMetaItem as _SchemaPluginDashboardMetaItem
from app.schemas.plugin import PluginDataSummary as _SchemaPluginDataSummary
from app.schemas.plugin import PluginInstallOutcome as _SchemaPluginInstallOutcome
from app.schemas.plugin import PluginRating as _SchemaPluginRating
from app.schemas.plugin import PluginRatingMap as _SchemaPluginRatingMap
from app.schemas.plugin import PluginRatingRequest as _SchemaPluginRatingRequest
from app.schemas.plugin import PluginReleaseData as _SchemaPluginReleaseData
from app.schemas.plugin import PluginRemoteInfo as _SchemaPluginRemoteInfo
from app.schemas.plugin import PluginRuntimeActionCapability as _SchemaPluginRuntimeActionCapability
from app.schemas.plugin import PluginRuntimeActionGroup as _SchemaPluginRuntimeActionGroup
from app.schemas.plugin import PluginRuntimeCapabilities as _SchemaPluginRuntimeCapabilities
from app.schemas.plugin import PluginRuntimeCommandCapability as _SchemaPluginRuntimeCommandCapability
from app.schemas.plugin import PluginRuntimeServiceCapability as _SchemaPluginRuntimeServiceCapability
from app.schemas.plugin import PluginRuntimeStatus as _SchemaPluginRuntimeStatus
from app.schemas.plugin import PluginRuntimeSummary as _SchemaPluginRuntimeSummary
from app.schemas.plugin import PluginSidebarNavItem as _SchemaPluginSidebarNavItem
from app.schemas.plugin import PluginSourceCandidate as _SchemaPluginSourceCandidate
from app.schemas.plugin import PluginSourceChangeRequest as _SchemaPluginSourceChangeRequest
from app.schemas.plugin import PluginSourceIdentity as _SchemaPluginSourceIdentity
from app.schemas.plugin import PluginSourceInstallRequest as _SchemaPluginSourceInstallRequest
from app.schemas.plugin import PluginSourceOptions as _SchemaPluginSourceOptions
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import SystemConfigKey
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()
router.routes.extend(plugin_folders_router.routes)
_plugin_release_refresh_tasks: set[asyncio.Task] = set()


def _plugin_source_identity_schema(identity: Any) -> _SchemaPluginSourceIdentity:
    """把持久化身份映射为公共来源确认 DTO。"""
    return _SchemaPluginSourceIdentity(
        plugin_id=identity.plugin_id,
        trusted_source_type=identity.trusted_source_type.value,
        trusted_source_key=identity.trusted_source_key,
        binding_basis=identity.binding_basis.value,
        payload_source_type=identity.payload_source_type.value,
        payload_source_key=identity.payload_source_key,
        revision=identity.revision,
    )


async def _refresh_plugin_release_versions(plugin_id: str, repo_url: str) -> None:
    """
    后台强制刷新 Release 缓存，接口响应路径优先返回已有缓存。
    """
    try:
        await get_plugin_release_service().refresh(plugin_id, repo_url)
    except Exception as e:
        logger.warning(f"后台刷新插件 {plugin_id} Release 列表失败：{e}")


def _schedule_plugin_release_refresh(plugin_id: str, repo_url: str, task_registry: TaskRegistry | None = None) -> None:
    """
    保留后台任务引用，避免任务被回收，同时让 helper 负责同仓库强刷合并。
    """
    registry = resolve_background_task_registry(task_registry)
    task = registry.create(
        _refresh_plugin_release_versions(plugin_id, repo_url),
        owner="api.plugin.release_refresh",
    )
    _plugin_release_refresh_tasks.add(task)

    def _discard_task(completed_task: asyncio.Task) -> None:
        _plugin_release_refresh_tasks.discard(completed_task)

    task.add_done_callback(_discard_task)


def register_plugin(plugin_id: str):
    """
    注册一个插件相关的服务
    """
    # 注册插件服务
    update_plugin_job(plugin_id)
    # 注册菜单命令
    init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)


def _is_plugin_auth_remote_file(plugin_id: str, filepath: str) -> bool:
    """
    判断静态文件是否属于插件声明的匿名登录认证远程组件。

    登录页加载插件认证组件时尚未产生登录态和资源 Cookie，因此仅对插件主动
    声明的认证 remote 保留匿名读取能力，其余插件静态资源仍需资源令牌。
    """
    path = filepath.lstrip("/")
    normalized_plugin_id = plugin_id.lower()
    plugin_manager = get_plugin_manager()
    for provider in plugin_manager.get_plugin_auth_providers():
        remote = provider.get("remote") or {}
        if str(remote.get("id") or "").lower() != normalized_plugin_id:
            continue
        remote_path = str(remote.get("url") or "").lstrip("/")
        remote_path_lower = remote_path.lower()
        expected_prefix = f"plugin/file/{normalized_plugin_id}/"
        if not remote_path_lower.startswith(expected_prefix):
            continue
        remote_file = remote_path[len(expected_prefix) :]
        remote_dir = remote_file.rsplit("/", 1)[0] if "/" in remote_file else ""
        if path == remote_file or (remote_dir and path.startswith(f"{remote_dir}/")):
            return True
    return False


def _verify_plugin_static_file_access(
    plugin_id: str,
    filepath: str,
    resource_token: Annotated[Optional[str], Security(resource_token_cookie)] = None,
) -> None:
    """
    校验插件静态文件访问权限。

    普通插件资源依赖登录后写入的资源 Cookie；登录认证插件的远程组件需要在
    登录前加载，因此仅对插件声明的认证 remote 放行匿名读取。
    """
    if _is_plugin_auth_remote_file(plugin_id, filepath):
        return
    verify_resource_token(resource_token)


@router.get(
    "/", summary="所有插件", response_model=List[_SchemaPlugin], openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True}
)
async def all_plugins(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    state: Optional[str] = "all",
    force: bool = False,
    query: Optional[str] = None,
    max_results: Annotated[Optional[int], Query(ge=1, le=200)] = None,
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
    response: Response = None,
) -> List[_SchemaPlugin]:
    """查询插件清单；未指定分页或限量时返回完整清单。"""
    plugins = await get_plugin_catalog_query().query(state=state or "all", force=force)
    if query:
        plugins = [item["plugin"] for item in search_plugin_candidates(query, plugins)]
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(len(plugins))
    if page is not None or count is not None:
        page, count = resolve_compatible_pagination(page, count)
        assert page is not None and count is not None
        return plugins[(page - 1) * count : page * count]
    return plugins if max_results is None else plugins[:max_results]


@router.get("/installed", summary="已安装插件", response_model=List[str])
async def installed(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询用户已安装插件清单
    """
    return get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []


@router.get(
    "/runtime",
    summary="插件运行时收敛状态",
    response_model=_SchemaPluginRuntimeSummary,
)
async def runtime_status(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaPluginRuntimeSummary:
    """返回插件页轮询所需的轻量状态摘要。"""
    plugin_manager = get_plugin_manager()
    statuses = plugin_manager.get_plugin_runtime_statuses()
    installed_plugin_ids = {
        plugin_id.lower()
        for plugin_id in (get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or [])
    }
    restart_requirements = {
        plugin_id: distributions
        for plugin_id, distributions in (plugin_manager.get_plugin_restart_requirements().items())
        if plugin_id.lower() in installed_plugin_ids
    }
    pending = {
        _SchemaPluginRuntimeStatus.SOURCE_MISSING,
        _SchemaPluginRuntimeStatus.DEPENDENCY_PENDING,
        _SchemaPluginRuntimeStatus.READY,
    }
    failed = {
        _SchemaPluginRuntimeStatus.BLOCKED_BY_POLICY,
        _SchemaPluginRuntimeStatus.LOAD_FAILED,
    }
    return _SchemaPluginRuntimeSummary(
        ready=not plugin_manager.is_plugin_settling(),
        generation=plugin_manager.get_plugin_runtime_generation(),
        pending_count=sum(status in pending for status in statuses.values()),
        failed_count=sum(status in failed for status in statuses.values()),
        restart_required_plugin_ids=sorted(restart_requirements),
    )


@router.get("/history/{plugin_id}", summary="获取插件更新说明", response_model=_SchemaPlugin)
async def plugin_history(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    force: bool = True,
) -> _SchemaPlugin:
    """
    按需获取指定插件的更新说明。
    """
    plugin = await get_plugin_release_service().history(plugin_id, force=force)
    if not plugin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"插件 {plugin_id} 不存在或未安装",
        )
    return plugin


@router.get(
    "/releases/{plugin_id}",
    summary="获取插件Release版本",
    response_model=_SchemaPluginReleaseData,
)
async def plugin_releases(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    repo_url: Optional[str] = "",
    force: bool = False,
    task_registry: TaskRegistry = Depends(get_background_task_registry),
) -> dict:
    """
    查询指定插件可直接安装的 GitHub Release 版本。

    市场元数据只读取请求仓库的当前 package，避免版本历史请求触发全部市场缓存读取。
    """
    snapshot = await get_plugin_release_service().versions(
        plugin_id,
        repo_url or "",
        force=force,
    )
    if snapshot.refresh_required:
        _schedule_plugin_release_refresh(
            plugin_id,
            repo_url or "",
            resolve_background_task_registry(task_registry),
        )
    return {
        "release_supported": snapshot.release_supported,
        "latest_version": snapshot.latest_version,
        "current_version": snapshot.current_version,
        "items": list(snapshot.items),
    }


@router.get(
    "/statistic",
    summary="插件安装统计",
    response_model=_SchemaJsonObject,
)
async def statistic(_: _SchemaTokenPayload = Depends(verify_token)) -> Any:
    """
    插件安装统计
    """
    return await get_plugin_rating_service().statistic()


@router.get(
    "/rating",
    summary="批量查询插件评分",
    response_model=_SchemaPluginRatingMap,
)
async def plugin_ratings(
    plugin_ids: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Dict[str, _SchemaPluginRating]:
    """
    批量查询插件平均分、评分人数和当前安装实例评分。
    """
    requested_ids = plugin_ids.split(",") if plugin_ids is not None else None
    ratings = await get_plugin_rating_service().ratings(requested_ids)
    return {plugin_id: _SchemaPluginRating.model_validate(rating) for plugin_id, rating in ratings.items()}


@router.get(
    "/rating/{plugin_id}",
    summary="查询插件评分",
    response_model=_SchemaPluginRating,
)
async def plugin_rating(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaPluginRating:
    """
    查询单个插件平均分、评分人数和当前安装实例评分。
    """
    rating = await get_plugin_rating_service().rating(plugin_id)
    return _SchemaPluginRating.model_validate(rating)


@router.post(
    "/rating/{plugin_id}",
    summary="提交插件评分",
    response_model=_SchemaResponse[_SchemaPluginRating],
)
async def rate_plugin(
    plugin_id: str,
    payload: _SchemaPluginRatingRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """
    为已安装插件新增或更新当前安装实例评分。
    """
    try:
        rating = await get_plugin_rating_service().submit(plugin_id, payload.rating)
    except PluginNotInstalledError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    if rating is None:
        return _SchemaResponse(success=False, message="连接MoviePilot服务器失败")
    return _SchemaResponse(success=True, data=rating)


@router.post("/reload/{plugin_id}", summary="重新加载插件", response_model=_SchemaResponse[None])
def reload_plugin(plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)) -> Any:
    """重新加载插件并刷新其命令、定时任务和动态 API 注册。"""
    try:
        runtime_status = reload_plugin_runtime(plugin_id)
    except PluginMutationRejectedError as error:
        return _SchemaResponse(success=False, message=str(error))
    if runtime_status is _SchemaPluginRuntimeStatus.ACTIVE:
        return _SchemaResponse(success=True)
    return _SchemaResponse(
        success=False,
        message=(
            "未通过用户认证，请查看日志"
            if runtime_status is _SchemaPluginRuntimeStatus.BLOCKED_BY_POLICY
            else "插件加载失败，请查看插件日志"
        ),
    )


@router.get(
    "/install/{plugin_id}",
    summary="安装插件",
    response_model=_SchemaResponse[_SchemaPluginInstallOutcome],
)
async def install(
    plugin_id: str,
    repo_url: Optional[str] = "",
    release_version: Optional[str] = None,
    force: Optional[bool] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    安装插件
    """
    result = await get_plugin_install_service().install(
        plugin_id=plugin_id,
        repo_url=repo_url or None,
        release_version=release_version,
        force=bool(force),
        explicit_source=False,
    )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(
        success=True,
        message=result.message,
        data=_SchemaPluginInstallOutcome(
            restart_required=result.restart_required,
        ),
    )


@router.get(
    "/source/{plugin_id}",
    summary="获取插件来源身份",
    response_model=_SchemaResponse[_SchemaPluginSourceIdentity],
)
async def get_plugin_source_identity(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """返回显式换源确认所需的当前可信来源和 revision。"""
    identity = await get_plugin_persistence().get_identity(plugin_id)
    if identity is None:
        return _SchemaResponse(success=False, message="未找到该插件的仓库绑定信息")
    return _SchemaResponse(
        success=True,
        data=_plugin_source_identity_schema(identity),
    )


@router.get(
    "/source/{plugin_id}/options",
    summary="获取插件来源候选",
    response_model=_SchemaResponse[_SchemaPluginSourceOptions],
)
async def get_plugin_source_options(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    force: bool = False,
) -> Any:
    """返回与真实安装相同库存中的脱敏候选和当前准入状态。"""
    inspection = await get_plugin_install_service().inspect_source(
        plugin_id=plugin_id,
        force=force,
    )
    candidates = [
        _SchemaPluginSourceCandidate.model_validate(candidate.public_dict())
        for candidate in inspection.online_candidates
    ]
    if inspection.local_candidate is not None:
        candidates.append(_SchemaPluginSourceCandidate.model_validate(inspection.local_candidate.public_dict()))
    return _SchemaResponse(
        success=True,
        data=_SchemaPluginSourceOptions(
            plugin_id=inspection.plugin_id,
            inventory_complete=inspection.inventory_complete,
            selection_status=inspection.selection.status.value,
            selection_reason=inspection.selection.reason,
            identity=(_plugin_source_identity_schema(inspection.identity) if inspection.identity is not None else None),
            candidates=candidates,
        ),
    )


@router.post(
    "/source/{plugin_id}/install",
    summary="按明确来源安装插件",
    response_model=_SchemaResponse[_SchemaPluginInstallOutcome],
)
async def install_plugin_from_source(
    plugin_id: str,
    request: _SchemaPluginSourceInstallRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """安装管理员明确选择的初始在线来源，不承担已绑定插件换源。"""
    result = await get_plugin_install_service().install(
        plugin_id=plugin_id,
        repo_url=request.repo_url,
        release_version=request.release_version,
        force=request.force,
        explicit_source=True,
    )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(
        success=True,
        message=result.message,
        data=_SchemaPluginInstallOutcome(
            restart_required=result.restart_required,
        ),
    )


@router.post(
    "/source/{plugin_id}",
    summary="切换插件来源",
    response_model=_SchemaResponse[_SchemaPluginInstallOutcome],
)
async def change_plugin_source(
    plugin_id: str,
    request: _SchemaPluginSourceChangeRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """按精确身份 revision 安装明确选择的新在线来源。"""
    result = await get_plugin_install_service().install(
        plugin_id=plugin_id,
        repo_url=request.repo_url,
        release_version=request.release_version,
        force=True,
        explicit_source=True,
        source_change=True,
        expected_revision=request.expected_revision,
    )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(
        success=True,
        message=result.message,
        data=_SchemaPluginInstallOutcome(
            restart_required=result.restart_required,
        ),
    )


@router.get("/remotes", summary="获取插件联邦组件列表", response_model=List[_SchemaPluginRemoteInfo])
async def remotes(token: str, page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    获取插件联邦组件列表
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")
    return get_plugin_manager().get_plugin_remotes()


@router.get("/sidebar_nav", summary="获取插件侧栏导航项", response_model=List[_SchemaPluginSidebarNavItem])
def plugin_sidebar_nav(
    _: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None
) -> Any:
    """
    聚合已启用 Vue 插件声明的侧栏入口（get_sidebar_nav），供前端主界面侧栏展示。
    """
    return get_plugin_manager().get_plugin_sidebar_nav()


@router.get(
    "/form/{plugin_id}",
    summary="获取插件表单页面",
    response_model=_SchemaJsonObject,
)
def plugin_form(plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件配置表单或Vue组件URL
    """
    plugin_manager = get_plugin_manager()
    plugin_instance = plugin_manager.running_plugins.get(plugin_id)
    if not plugin_instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"插件 {plugin_id} 不存在或未加载",
        )

    # 渲染模式
    render_mode, _ = plugin_instance.get_render_mode()
    try:
        conf, model = plugin_instance.get_form()
        stored_config = plugin_manager.get_plugin_config(plugin_id)
        # Merge stored config with defaults so all keys exist for v-show evaluation
        merged_model = {**model, **(stored_config or {})}
        return {
            "render_mode": render_mode,
            "conf": conf,
            "model": merged_model,
        }
    except Exception as e:
        logger.error(f"插件 {plugin_id} 调用方法 get_form 出错: {str(e)}")
    return {}


@router.get(
    "/page/{plugin_id}",
    summary="获取插件数据页面",
    response_model=_SchemaJsonObject,
)
def plugin_page(plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件数据页面
    """
    plugin_instance = get_plugin_manager().running_plugins.get(plugin_id)
    if not plugin_instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"插件 {plugin_id} 不存在或未加载",
        )

    # 渲染模式
    render_mode, _ = plugin_instance.get_render_mode()
    try:
        page = plugin_instance.get_page()
        return {"render_mode": render_mode, "page": page or []}
    except Exception as e:
        logger.error(f"插件 {plugin_id} 调用方法 get_page 出错: {str(e)}")
    return {}


@router.get("/dashboard/meta", summary="获取所有插件仪表板元信息", response_model=List[_SchemaPluginDashboardMetaItem])
def plugin_dashboard_meta(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[dict]:
    """
    获取所有插件仪表板元信息
    """
    return get_plugin_manager().get_plugin_dashboard_meta()


@router.get("/dashboard/{plugin_id}/{key}", summary="获取插件仪表板配置")
def plugin_dashboard_by_key(
    plugin_id: str,
    key: str,
    user_agent: Annotated[str | None, Header()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Optional[_SchemaPluginDashboard]:
    """
    根据插件ID获取插件仪表板
    """
    try:
        return get_plugin_manager().get_plugin_dashboard(plugin_id, key, user_agent)
    except PluginNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PluginDashboardError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/dashboard/{plugin_id}", summary="获取插件仪表板配置")
def plugin_dashboard(
    plugin_id: str,
    user_agent: Annotated[str | None, Header()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Optional[_SchemaPluginDashboard]:
    """
    根据插件ID获取插件仪表板
    """
    return plugin_dashboard_by_key(plugin_id, "", user_agent)


@router.get("/reset/{plugin_id}", summary="重置插件配置及数据", response_model=_SchemaResponse[None])
def reset_plugin(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    command: PluginConfigCommand = Depends(get_plugin_config_command),
) -> Any:
    """
    根据插件ID重置插件配置及数据
    """
    result = command.reset(plugin_id)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(
    "/file/{plugin_id}/{filepath:path}",
    summary="获取插件静态文件",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "插件静态资源",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                "application/javascript": {"schema": {"type": "string"}},
                "text/css": {"schema": {"type": "string"}},
            },
        }
    },
)
async def plugin_static_file(
    plugin_id: str,
    filepath: str,
    _: None = Depends(_verify_plugin_static_file_access),
) -> StreamingResponse:
    """
    获取插件静态文件
    """
    # 基础安全检查
    if ".." in filepath or ".." in plugin_id:
        logger.warning(f"Static File API: Path traversal attempt detected: {plugin_id}/{filepath}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    source_plugin_id = get_plugin_manager().get_plugin_source_id(plugin_id)
    plugin_base_dir = (
        AsyncPath(get_api_runtime_config_snapshot().root_path) / "app" / "plugins" / source_plugin_id.lower()
    )
    plugin_file_path = plugin_base_dir / filepath.lstrip("/")

    try:
        resolved_base = await plugin_base_dir.resolve()
        resolved_file = await plugin_file_path.resolve()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")

    if not resolved_file.is_relative_to(resolved_base):
        logger.warning(f"Static File API: Path traversal attempt detected: {plugin_id}/{filepath}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if not await plugin_file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{plugin_file_path} 不存在")
    if not await plugin_file_path.is_file():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{plugin_file_path} 不是文件")

    # 判断 MIME 类型
    response_type, _ = mimetypes.guess_type(str(plugin_file_path))
    suffix = plugin_file_path.suffix.lower()
    # 强制修正 .mjs 和 .js 的 MIME 类型
    if suffix in [".js", ".mjs"]:
        response_type = "application/javascript"
    elif suffix == ".css" and not response_type:  # 如果 guess_type 没猜对 css，也修正
        response_type = "text/css"
    elif not response_type:  # 对于其他猜不出的类型
        response_type = "application/octet-stream"

    try:
        # 异步生成器函数，用于流式读取文件
        async def file_generator():
            async with aiofiles.open(plugin_file_path, mode="rb") as file:
                # 8KB 块大小
                while chunk := await file.read(8192):
                    yield chunk

        return StreamingResponse(
            file_generator(),
            media_type=response_type,
            headers={"Content-Disposition": f"inline; filename={plugin_file_path.name}"},
        )
    except Exception as e:
        logger.error(
            f"Error creating/sending StreamingResponse for {plugin_file_path}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.post("/clone/{plugin_id}", summary="创建插件分身", response_model=_SchemaResponse[None])
def clone_plugin(
    plugin_id: str,
    clone_data: _SchemaPluginCloneRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    创建插件分身
    """
    plugin_manager = get_plugin_manager()
    try:
        with plugin_manager.mutation(f"创建插件 {plugin_id} 分身"):
            success, message = plugin_manager.clone_plugin(
                plugin_id=plugin_id,
                suffix=clone_data.suffix,
                name=clone_data.name,
                description=clone_data.description,
                version=clone_data.version,
                icon=clone_data.icon,
            )

            if success:
                # 分身服务已完成运行态加载，此处只补齐宿主注册。
                register_plugin(message)
                # 将分身插件添加到原插件所在的文件夹中
                add_clone_to_plugin_folder(plugin_id, message)
                return _SchemaResponse(success=True, message="插件分身创建成功")
            return _SchemaResponse(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return _SchemaResponse(success=False, message=f"创建插件分身失败：{str(e)}")


@router.get(  # type: ignore[misc]
    "/runtime/capabilities",
    summary="查询插件运行能力",
    response_model=_SchemaResponse[_SchemaPluginRuntimeCapabilities],
)
async def plugin_capabilities(
    plugin_id: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[Any]:
    """查询运行中插件注册的安全命令、动作和定时服务元数据。"""
    manager = get_plugin_manager()
    commands = [
        _SchemaPluginRuntimeCommandCapability(
            cmd=str(command["cmd"]),
            desc=str(command["desc"]) if command.get("desc") else None,
            plugin_id=str(command["pid"]) if command.get("pid") else None,
        )
        for command in (manager.get_plugin_commands(pid=plugin_id) or [])
        if isinstance(command, dict) and command.get("cmd")
    ]
    action_groups = []
    for group in manager.get_plugin_actions(pid=plugin_id) or []:
        if not isinstance(group, dict):
            continue
        actions = [
            _SchemaPluginRuntimeActionCapability(
                id=str(item["id"]),
                name=str(item["name"]) if item.get("name") else None,
            )
            for item in (group.get("actions") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        if actions:
            action_groups.append(
                _SchemaPluginRuntimeActionGroup(
                    plugin_id=str(group["plugin_id"]) if group.get("plugin_id") else None,
                    plugin_name=str(group["plugin_name"]) if group.get("plugin_name") else None,
                    actions=actions,
                )
            )
    services = [
        _SchemaPluginRuntimeServiceCapability(
            id=str(service["id"]),
            name=str(service["name"]) if service.get("name") else None,
            trigger=str(service["trigger"]) if service.get("trigger") else None,
        )
        for service in (manager.get_plugin_services(pid=plugin_id) or [])
        if isinstance(service, dict) and service.get("id")
    ]
    return _SchemaResponse(
        success=True,
        data=_SchemaPluginRuntimeCapabilities(
            commands=commands,
            actions=action_groups,
            services=services,
        ),
    )


@router.get(  # type: ignore[misc]
    "/runtime/{plugin_id}/data",
    summary="查询插件持久化数据",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def plugin_data(
    plugin_id: str,
    key: Optional[str] = None,
    max_chars: Optional[int] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """读取插件单键或全部持久化数据，并对大结果进行裁剪。"""
    try:
        data = await PluginDataQueryService(
            runtime.agent.plugin_data,
            get_plugin_snapshot,
        ).query(plugin_id, key=key, max_chars=max_chars)
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.get(
    "/runtime/{plugin_id}/data/summary",
    summary="查询插件持久化数据摘要",
    response_model=_SchemaResponse[_SchemaPluginDataSummary],
)
async def plugin_data_summary(
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """读取不包含插件持久化原值的键、类型和大小摘要。"""
    try:
        data = await PluginDataSummaryService(
            runtime.agent.plugin_data,
            get_plugin_snapshot,
        ).summarize(plugin_id)
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=_SchemaPluginDataSummary.model_validate(data))


@router.get(
    "/{plugin_id}",
    summary="获取插件配置",
    response_model=_SchemaJsonObject,
)
async def plugin_config(plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return get_plugin_manager().get_plugin_config(plugin_id)


@router.put("/{plugin_id}", summary="更新插件配置", response_model=_SchemaResponse[None])
def set_plugin_config(
    plugin_id: str,
    conf: dict,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    command: PluginConfigCommand = Depends(get_plugin_config_command),
) -> Any:
    """
    更新插件配置
    """
    result = command.update(plugin_id, conf)
    return _SchemaResponse(success=result.success, message=result.message)


@router.delete("/{plugin_id}", summary="卸载插件", response_model=_SchemaResponse[None])
def uninstall_plugin(plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)) -> Any:
    """
    卸载插件
    """
    plugin_manager = get_plugin_manager()
    try:
        with plugin_manager.mutation(f"卸载插件 {plugin_id}"):
            virtual_instance = plugin_manager.get_plugin_instance(plugin_id)
            source_instances = plugin_manager.get_plugin_source_instances(plugin_id)
            if not virtual_instance and source_instances:
                instance_ids = "、".join(item.instance_id for item in source_instances)
                return _SchemaResponse(
                    success=False,
                    message=f"请先卸载该插件的分身：{instance_ids}",
                )
            config_oper = get_configured_system_config()
            # 删除已安装信息
            install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
            for plugin in install_plugins:
                if plugin == plugin_id:
                    install_plugins.remove(plugin)
                    break
            config_oper.set(SystemConfigKey.UserInstalledPlugins, install_plugins)
            # 移除插件API
            remove_plugin_api(plugin_id)
            # 移除插件服务
            remove_plugin_job(plugin_id)
            # 判断是否为分身
            plugin_class = plugin_manager.plugins.get(plugin_id)
            # 删除必须晚于停止：停机钩子会重建刚删的自有库；停止同时注销插件类，故删除一律按 force
            plugin_manager.stop(plugin_id)
            if virtual_instance:
                plugin_manager.delete_plugin_config(plugin_id, force=True)
                plugin_manager.delete_plugin_data(plugin_id, force=True)
                plugin_manager.delete_plugin_instance(plugin_id)
            elif getattr(plugin_class, "is_clone", False):
                plugin_manager.delete_plugin_config(plugin_id, force=True)
                plugin_manager.delete_plugin_data(plugin_id, force=True)
                # 分身物理目录只能由包文件 owner 删除。
                if plugin_manager.remove_plugin_package(plugin_id):
                    plugin_manager.plugins.pop(plugin_id, None)
            # 从插件文件夹中移除该插件
            remove_plugin_from_folders(plugin_id)
            # 移除插件
            plugin_manager.remove_plugin(plugin_id)
            return _SchemaResponse(success=True)
    except PluginMutationRejectedError as error:
        return _SchemaResponse(success=False, message=str(error))
