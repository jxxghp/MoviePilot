import asyncio
import mimetypes
import shutil
from typing import Annotated, Any, Dict, List, Optional

import aiofiles
from anyio import Path as AsyncPath
from fastapi import Depends, Header, HTTPException, Security
from fastapi.concurrency import run_in_threadpool
from starlette import status
from starlette.responses import StreamingResponse

from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.plugin import Plugin as _SchemaPlugin
from app.schemas.plugin import PluginDashboard as _SchemaPluginDashboard
from app.schemas.plugin import PluginCloneRequest as _SchemaPluginCloneRequest
from app.schemas.plugin import PluginDashboardMetaItem as _SchemaPluginDashboardMetaItem
from app.schemas.plugin import PluginFoldersData as _SchemaPluginFoldersData
from app.schemas.plugin import PluginRating as _SchemaPluginRating
from app.schemas.plugin import PluginRatingMap as _SchemaPluginRatingMap
from app.schemas.plugin import PluginRatingRequest as _SchemaPluginRatingRequest
from app.schemas.plugin import PluginReleaseData as _SchemaPluginReleaseData
from app.schemas.plugin import PluginRemoteInfo as _SchemaPluginRemoteInfo
from app.schemas.plugin import PluginRuntimeStatus as _SchemaPluginRuntimeStatus
from app.schemas.plugin import PluginRuntimeSummary as _SchemaPluginRuntimeSummary
from app.schemas.plugin import PluginSidebarNavItem as _SchemaPluginSidebarNavItem
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.api.response import ResponseAPIRouter
from app.application.plugin.folders import remove_plugin_from_folders
from app.application.plugin.routes import register_plugin_api, remove_plugin_api
from app.application.plugin.install import PluginInstallCommand
from app.application.plugin.config import PluginConfigCommand
from app.application.commands import init_commands
from app.application.scheduling import remove_plugin_job, update_plugin_job
from app.runtime.cache import async_fresh
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.plugin.runtime import get_plugin_manager as PluginManager
from app.runtime.extensions.plugin.contracts import (
    PluginDashboardError,
    PluginNotFoundError,
)
from app.adapters.web.security.access import (
    resource_token_cookie,
    verify_resource_token,
    verify_token,
)
from app.api.principal import ApiPrincipal
from app.application.configuration import get_configured_system_config
from app.api.dependencies.auth import (
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.api.dependencies.plugin import (
    get_plugin_config_command,
)
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.external.market import PluginHelper
from app.adapters.system.plugin.package import PluginPackageManager
from app.application.database import DatabaseWorkerOverloadedError
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

router = ResponseAPIRouter()
_plugin_release_refresh_tasks: set[asyncio.Task] = set()


async def _get_market_plugin_from_repo(
    plugin_manager: PluginManager,
    plugin_id: str,
    repo_url: str,
    force: bool,
) -> Optional[_SchemaPlugin]:
    """
    只读取指定插件仓库的市场元数据，避免单插件详情触发全部市场刷新。
    """
    market_plugins = await plugin_manager.async_get_plugins_from_market(
        repo_url, get_api_runtime_config_snapshot().version_flag, force
    )
    market_plugin = next(
        (
            plugin
            for plugin in market_plugins or []
            if plugin.id == plugin_id
        ),
        None,
    )
    if market_plugin or not get_api_runtime_config_snapshot().version_flag:
        return market_plugin

    compatible_plugins = await plugin_manager.async_get_plugins_from_market(
        repo_url, None, force
    )
    return next(
        (
            plugin
            for plugin in compatible_plugins or []
            if plugin.id == plugin_id
        ),
        None,
    )


async def _refresh_plugin_release_versions(plugin_id: str, repo_url: str) -> None:
    """
    后台强制刷新 Release 缓存，接口响应路径优先返回已有缓存。
    """
    try:
        async with async_fresh(True):
            await PluginHelper().async_get_plugin_release_versions(plugin_id, repo_url)
    except Exception as e:
        logger.warning(f"后台刷新插件 {plugin_id} Release 列表失败：{e}")


def _schedule_plugin_release_refresh(plugin_id: str, repo_url: str) -> None:
    """
    保留后台任务引用，避免任务被回收，同时让 helper 负责同仓库强刷合并。
    """
    task = asyncio.create_task(_refresh_plugin_release_versions(plugin_id, repo_url))
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


def _merge_plugin_market_metadata(
    plugin: _SchemaPlugin, market_plugin: _SchemaPlugin
) -> _SchemaPlugin:
    """
    合并插件市场中的远端元数据，供已安装插件按需展示更新说明。
    """
    plugin.repo_url = market_plugin.repo_url or plugin.repo_url
    plugin.history = market_plugin.history or {}
    plugin.release = market_plugin.release
    plugin.has_update = market_plugin.has_update
    plugin.system_version = market_plugin.system_version or plugin.system_version
    plugin.system_version_compatible = market_plugin.system_version_compatible
    plugin.system_version_message = (
        market_plugin.system_version_message or plugin.system_version_message
    )
    return plugin


def _is_plugin_auth_remote_file(plugin_id: str, filepath: str) -> bool:
    """
    判断静态文件是否属于插件声明的匿名登录认证远程组件。

    登录页加载插件认证组件时尚未产生登录态和资源 Cookie，因此仅对插件主动
    声明的认证 remote 保留匿名读取能力，其余插件静态资源仍需资源令牌。
    """
    path = filepath.lstrip("/")
    normalized_plugin_id = plugin_id.lower()
    plugin_manager = PluginManager()
    for provider in plugin_manager.get_plugin_auth_providers():
        remote = provider.get("remote") or {}
        if str(remote.get("id") or "").lower() != normalized_plugin_id:
            continue
        remote_path = str(remote.get("url") or "").lstrip("/")
        remote_path_lower = remote_path.lower()
        expected_prefix = f"plugin/file/{normalized_plugin_id}/"
        if not remote_path_lower.startswith(expected_prefix):
            continue
        remote_file = remote_path[len(expected_prefix):]
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


async def _get_plugin_history_detail(
    plugin_id: str, force: bool = True
) -> Optional[_SchemaPlugin]:
    """
    按需获取插件远端元数据，避免插件列表加载时批量访问网络。
    """
    plugin_manager = PluginManager()
    installed_plugin = next(
        (
            plugin
            for plugin in plugin_manager.get_local_plugins()
            if plugin.id == plugin_id and plugin.installed
        ),
        None,
    )
    if not installed_plugin:
        return None

    local_repo_plugin = next(
        (plugin for plugin in plugin_manager.get_local_repo_plugins() if plugin.id == plugin_id),
        None,
    )
    if local_repo_plugin:
        return _merge_plugin_market_metadata(installed_plugin, local_repo_plugin)

    if installed_plugin.repo_url:
        market_plugin = await _get_market_plugin_from_repo(
            plugin_manager, plugin_id, installed_plugin.repo_url, force
        )
        if not market_plugin:
            logger.debug(f"插件 {plugin_id} 未从来源仓库获取到更新说明，返回本地插件信息")
            return installed_plugin
        return _merge_plugin_market_metadata(installed_plugin, market_plugin)

    market_plugin = next(
        (
            plugin
            for plugin in await plugin_manager.async_get_online_plugins(force=force)
            if plugin.id == plugin_id
        ),
        None,
    )
    if not market_plugin:
        return installed_plugin

    return _merge_plugin_market_metadata(installed_plugin, market_plugin)


@router.get("/", summary="所有插件", response_model=List[_SchemaPlugin])
async def all_plugins(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    state: Optional[str] = "all",
    force: bool = False,
) -> List[_SchemaPlugin]:
    """
    查询所有插件清单，包括本地插件和在线插件，插件状态：installed, market, all
    """
    # 本地插件
    plugin_manager = PluginManager()
    local_plugins = plugin_manager.get_local_plugins()
    # 已安装插件
    installed_plugins = [plugin for plugin in local_plugins if plugin.installed]
    if state == "installed":
        return plugin_manager.get_installed_plugins()

    # 未安装的本地插件
    not_installed_plugins = [plugin for plugin in local_plugins if not plugin.installed]
    # 本地插件仓库目录中的插件
    local_repo_plugins = plugin_manager.get_local_repo_plugins()
    # 在线插件
    online_plugins = await plugin_manager.async_get_online_plugins(force)
    candidate_plugins = (
        plugin_manager.process_plugins_list(online_plugins + local_repo_plugins, [])
        if online_plugins or local_repo_plugins
        else []
    )
    if not candidate_plugins:
        # 没有获取在线插件
        if state == "market":
            # 返回未安装的本地插件
            return not_installed_plugins
        return local_plugins

    # 插件市场插件清单
    market_plugins = []
    # 已安装插件IDS
    _installed_ids = [plugin.id for plugin in installed_plugins]
    # 未安装的线上插件或者有更新的插件
    for plugin in candidate_plugins:
        if plugin.id not in _installed_ids:
            market_plugins.append(plugin)
        elif plugin.has_update:
            market_plugins.append(plugin)
    # 未安装的本地插件，且不在线上插件中
    _plugin_ids = [plugin.id for plugin in market_plugins]
    for plugin in not_installed_plugins:
        if plugin.id not in _plugin_ids:
            market_plugins.append(plugin)
    # 返回插件清单
    if state == "market":
        # 返回未安装的插件
        return market_plugins

    # 返回所有插件
    return installed_plugins + market_plugins


@router.get("/installed", summary="已安装插件", response_model=List[str])
async def installed(_: ApiPrincipal = Depends(get_current_active_superuser_async)) -> Any:
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
    plugin_manager = PluginManager()
    statuses = plugin_manager.get_plugin_runtime_statuses()
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
    plugin = await _get_plugin_history_detail(plugin_id=plugin_id, force=force)
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
) -> dict:
    """
    查询指定插件可直接安装的 GitHub Release 版本。

    市场元数据只读取请求仓库的当前 package，避免版本历史请求触发全部市场缓存读取。
    """
    if not repo_url:
        return {
            "release_supported": False,
            "latest_version": None,
            "current_version": None,
            "items": [],
        }

    plugin_manager = PluginManager()
    market_plugin = await _get_market_plugin_from_repo(
        plugin_manager, plugin_id, repo_url, force
    )
    latest_version = market_plugin.plugin_version if market_plugin else None
    current_version = plugin_manager.get_local_plugin_version(plugin_id)
    if not getattr(market_plugin, "release", False):
        return {
            "release_supported": False,
            "latest_version": latest_version,
            "current_version": current_version,
            "items": [],
        }

    plugin_helper = PluginHelper()
    has_release_cache = (
        await plugin_helper.async_has_plugin_release_cache(repo_url)
        if force
        else False
    )
    release_items = await plugin_helper.async_get_plugin_release_versions(plugin_id, repo_url)
    if force and has_release_cache:
        _schedule_plugin_release_refresh(plugin_id, repo_url)
    items = []
    for item in release_items:
        version = item.get("version")
        copied_item = item.copy()
        copied_item["is_latest"] = bool(latest_version and version == latest_version)
        copied_item["is_current"] = bool(current_version and version == current_version)
        items.append(copied_item)

    return {
        "release_supported": bool(items),
        "latest_version": latest_version,
        "current_version": current_version,
        "items": items,
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
    return await MoviePilotServerHelper.async_get_plugin_statistic()


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
    ratings = await MoviePilotServerHelper.async_get_plugin_ratings(requested_ids)
    return {
        plugin_id: _SchemaPluginRating.model_validate(rating)
        for plugin_id, rating in ratings.items()
    }


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
    rating = await MoviePilotServerHelper.async_get_plugin_rating(plugin_id)
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
    installed_plugins = get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
    if plugin_id not in installed_plugins:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"插件 {plugin_id} 未安装，无法评分",
        )

    rating = await MoviePilotServerHelper.async_submit_plugin_rating(
        plugin_id,
        payload.rating,
    )
    if rating is None:
        return _SchemaResponse(success=False, message="连接MoviePilot服务器失败")
    return _SchemaResponse(success=True, data=rating)


@router.get(
    "/reload/{plugin_id}", summary="重新加载插件", response_model=_SchemaResponse[None]
)
def reload_plugin(
    plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> Any:
    """
    重新加载插件
    """
    # 重新加载插件
    runtime_status = PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    register_plugin(plugin_id)
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


@router.get("/install/{plugin_id}", summary="安装插件", response_model=_SchemaResponse[None])
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
    plugin_helper = PluginHelper()
    package_manager = PluginPackageManager(plugin_helper)
    plugin_manager = PluginManager()

    async def save_installed_plugins(plugin_ids: List[str]) -> object:
        """保存安装用例确认后的插件列表。"""
        return await get_configured_system_config().async_set(
            SystemConfigKey.UserInstalledPlugins,
            plugin_ids,
        )

    async def install_package(
        target_id: str,
        target_repo: str,
        target_release: Optional[str],
        force_install: bool,
    ) -> tuple[bool, str]:
        """调用插件包适配器执行异步安装。"""
        return await package_manager.async_install(
            plugin_id=target_id,
            repo_url=target_repo,
            release_version=target_release,
            force_install=force_install,
        )

    async def reload_runtime(target_id: str) -> object:
        """在线程池中重建源插件及其全部虚拟实例。"""
        return await run_in_threadpool(PluginManager().reload_plugin_tree, target_id)

    async def refresh_registrations(target_id: str) -> object:
        """在线程池中刷新源插件及其虚拟实例的全部宿主注册。"""
        for reload_target in plugin_manager.get_plugin_reload_targets(target_id):
            await run_in_threadpool(register_plugin, reload_target)

    command = PluginInstallCommand(
        installed_plugins_reader=lambda: get_configured_system_config().get(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        installed_plugins_writer=save_installed_plugins,
        plugin_ids_provider=lambda: PluginManager().get_plugin_ids(),
        compatibility_checker=plugin_helper.async_get_plugin_system_version_check_message,
        package_installer=install_package,
        package_checkpointer=package_manager.async_checkpoint,
        package_committer=package_manager.async_commit,
        package_rollback=package_manager.async_rollback,
        install_reporter=lambda target_id, target_repo: (
            MoviePilotServerHelper.async_install_plugin_reg(
                plugin_id=target_id,
                repo_url=target_repo,
            )
        ),
        plugin_reloader=reload_runtime,
        registration_refresher=refresh_registrations,
    )
    with plugin_manager.suppress_plugin_monitor(plugin_id):
        result = await command.execute(
            plugin_id=plugin_id,
            repo_url=repo_url,
            release_version=release_version,
            force=bool(force),
        )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(success=True)


@router.get(
    "/remotes",
    summary="获取插件联邦组件列表",
    response_model=List[_SchemaPluginRemoteInfo],
)
async def remotes(token: str) -> Any:
    """
    获取插件联邦组件列表
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")
    return PluginManager().get_plugin_remotes()


@router.get(
    "/sidebar_nav",
    summary="获取插件侧栏导航项",
    response_model=List[_SchemaPluginSidebarNavItem],
)
def plugin_sidebar_nav(_: _SchemaTokenPayload = Depends(verify_token)) -> Any:
    """
    聚合已启用 Vue 插件声明的侧栏入口（get_sidebar_nav），供前端主界面侧栏展示。
    """
    return PluginManager().get_plugin_sidebar_nav()


@router.get(
    "/form/{plugin_id}",
    summary="获取插件表单页面",
    response_model=_SchemaJsonObject,
)
def plugin_form(
    plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> dict:
    """
    根据插件ID获取插件配置表单或Vue组件URL
    """
    plugin_manager = PluginManager()
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
def plugin_page(
    plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> dict:
    """
    根据插件ID获取插件数据页面
    """
    plugin_instance = PluginManager().running_plugins.get(plugin_id)
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


@router.get(
    "/dashboard/meta",
    summary="获取所有插件仪表板元信息",
    response_model=List[_SchemaPluginDashboardMetaItem],
)
def plugin_dashboard_meta(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> List[dict]:
    """
    获取所有插件仪表板元信息
    """
    return PluginManager().get_plugin_dashboard_meta()


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
        return PluginManager().get_plugin_dashboard(plugin_id, key, user_agent)
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


@router.get(
    "/reset/{plugin_id}", summary="重置插件配置及数据", response_model=_SchemaResponse[None]
)
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
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
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
        logger.warning(
            f"Static File API: Path traversal attempt detected: {plugin_id}/{filepath}"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    source_plugin_id = PluginManager().get_plugin_source_id(plugin_id)
    plugin_base_dir = (
        AsyncPath(get_api_runtime_config_snapshot().root_path)
        / "app"
        / "plugins"
        / source_plugin_id.lower()
    )
    plugin_file_path = plugin_base_dir / filepath.lstrip("/")

    try:
        resolved_base = await plugin_base_dir.resolve()
        resolved_file = await plugin_file_path.resolve()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        )

    if not resolved_file.is_relative_to(resolved_base):
        logger.warning(
            f"Static File API: Path traversal attempt detected: {plugin_id}/{filepath}"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if not await plugin_file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{plugin_file_path} 不存在"
        )
    if not await plugin_file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"{plugin_file_path} 不是文件"
        )

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
            headers={
                "Content-Disposition": f"inline; filename={plugin_file_path.name}"
            },
        )
    except Exception as e:
        logger.error(
            f"Error creating/sending StreamingResponse for {plugin_file_path}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get(
    "/folders",
    summary="获取插件文件夹配置",
    response_model=_SchemaPluginFoldersData,
)
async def get_plugin_folders(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> dict:
    """
    获取插件文件夹分组配置
    """
    try:
        result = get_configured_system_config().get(SystemConfigKey.PluginFolders) or {}
        return result
    except Exception as e:
        logger.error(f"[文件夹API] 获取文件夹配置失败: {str(e)}")
        return {}


@router.post("/folders", summary="保存插件文件夹配置", response_model=_SchemaResponse[None])
async def save_plugin_folders(
    folders: dict, _: ApiPrincipal = Depends(get_current_active_superuser_async)
) -> Any:
    """
    保存插件文件夹分组配置
    """
    try:
        await get_configured_system_config().async_set(
            SystemConfigKey.PluginFolders,
            folders,
        )
        return _SchemaResponse(success=True)
    except DatabaseWorkerOverloadedError:
        raise
    except Exception as e:
        logger.error(f"[文件夹API] 保存文件夹配置失败: {str(e)}")
        return _SchemaResponse(success=False, message=str(e))


@router.post(
    "/folders/{folder_name}", summary="创建插件文件夹", response_model=_SchemaResponse[None]
)
async def create_plugin_folder(
    folder_name: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)
) -> Any:
    """
    创建新的插件文件夹
    """
    folders = get_configured_system_config().get(SystemConfigKey.PluginFolders) or {}
    if folder_name not in folders:
        folders[folder_name] = []
        await get_configured_system_config().async_set(
            SystemConfigKey.PluginFolders,
            folders,
        )
        return _SchemaResponse(
            success=True, message=f"文件夹 '{folder_name}' 创建成功"
        )
    else:
        return _SchemaResponse(success=False, message=f"文件夹 '{folder_name}' 已存在")


@router.delete(
    "/folders/{folder_name}", summary="删除插件文件夹", response_model=_SchemaResponse[None]
)
async def delete_plugin_folder(
    folder_name: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)
) -> Any:
    """
    删除插件文件夹
    """
    folders = get_configured_system_config().get(SystemConfigKey.PluginFolders) or {}
    if folder_name in folders:
        del folders[folder_name]
        await get_configured_system_config().async_set(SystemConfigKey.PluginFolders, folders)
        return _SchemaResponse(
            success=True, message=f"文件夹 '{folder_name}' 删除成功"
        )
    else:
        return _SchemaResponse(success=False, message=f"文件夹 '{folder_name}' 不存在")


@router.put(
    "/folders/{folder_name}/plugins",
    summary="更新文件夹中的插件",
    response_model=_SchemaResponse[None],
)
async def update_folder_plugins(
    folder_name: str,
    plugin_ids: List[str],
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    更新指定文件夹中的插件列表
    """
    folders = get_configured_system_config().get(SystemConfigKey.PluginFolders) or {}
    folders[folder_name] = plugin_ids
    await get_configured_system_config().async_set(SystemConfigKey.PluginFolders, folders)
    return _SchemaResponse(
        success=True, message=f"文件夹 '{folder_name}' 中的插件已更新"
    )


@router.post(
    "/clone/{plugin_id}", summary="创建插件分身", response_model=_SchemaResponse[None]
)
def clone_plugin(
    plugin_id: str,
    clone_data: _SchemaPluginCloneRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    创建插件分身
    """
    try:
        success, message = PluginManager().clone_plugin(
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
            _add_clone_to_plugin_folder(plugin_id, message)
            return _SchemaResponse(success=True, message="插件分身创建成功")
        else:
            return _SchemaResponse(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return _SchemaResponse(success=False, message=f"创建插件分身失败：{str(e)}")


@router.get(
    "/{plugin_id}",
    summary="获取插件配置",
    response_model=_SchemaJsonObject,
)
async def plugin_config(
    plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)
) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_config(plugin_id)


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
def uninstall_plugin(
    plugin_id: str, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> Any:
    """
    卸载插件
    """
    plugin_manager = PluginManager()
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
    if virtual_instance:
        plugin_manager.delete_plugin_config(plugin_id, force=True)
        plugin_manager.delete_plugin_data(plugin_id, force=True)
        plugin_manager.delete_plugin_instance(plugin_id)
    elif getattr(plugin_class, "is_clone", False):
        # 如果是分身插件，则删除分身数据和配置
        plugin_manager.delete_plugin_config(plugin_id)
        plugin_manager.delete_plugin_data(plugin_id)
        # 删除分身文件
        plugin_base_dir = (
            get_api_runtime_config_snapshot().root_path
            / "app"
            / "plugins"
            / plugin_id.lower()
        )
        if plugin_base_dir.exists():
            try:
                shutil.rmtree(plugin_base_dir)
                plugin_manager.plugins.pop(plugin_id, None)
            except Exception as e:
                logger.error(f"删除插件分身目录 {plugin_base_dir} 失败: {str(e)}")
    # 从插件文件夹中移除该插件
    remove_plugin_from_folders(plugin_id)
    # 移除插件
    plugin_manager.remove_plugin(plugin_id)
    return _SchemaResponse(success=True)


def _add_clone_to_plugin_folder(original_plugin_id: str, clone_plugin_id: str):
    """
    将分身插件添加到原插件所在的文件夹中
    :param original_plugin_id: 原插件ID
    :param clone_plugin_id: 分身插件ID
    """
    try:
        config_oper = get_configured_system_config()
        # 获取插件文件夹配置
        folders = config_oper.get(SystemConfigKey.PluginFolders) or {}

        # 查找原插件所在的文件夹
        target_folder = None
        for folder_name, folder_data in folders.items():
            if isinstance(folder_data, dict) and "plugins" in folder_data:
                # 新格式：{"plugins": [...], "order": ..., "icon": ...}
                if original_plugin_id in folder_data["plugins"]:
                    target_folder = folder_name
                    break
            elif isinstance(folder_data, list):
                # 旧格式：直接是插件列表
                if original_plugin_id in folder_data:
                    target_folder = folder_name
                    break

        # 如果找到了原插件所在的文件夹，则将分身插件也添加到该文件夹中
        if target_folder:
            folder_data = folders[target_folder]
            if isinstance(folder_data, dict) and "plugins" in folder_data:
                # 新格式
                if clone_plugin_id not in folder_data["plugins"]:
                    folder_data["plugins"].append(clone_plugin_id)
                    logger.info(
                        f"已将分身插件 {clone_plugin_id} 添加到文件夹 '{target_folder}' 中"
                    )
            elif isinstance(folder_data, list):
                # 旧格式
                if clone_plugin_id not in folder_data:
                    folder_data.append(clone_plugin_id)
                    logger.info(
                        f"已将分身插件 {clone_plugin_id} 添加到文件夹 '{target_folder}' 中"
                    )

            # 保存更新后的文件夹配置
            config_oper.set(SystemConfigKey.PluginFolders, folders)
        else:
            logger.info(
                f"原插件 {original_plugin_id} 不在任何文件夹中，分身插件 {clone_plugin_id} 将保持独立"
            )

    except Exception as e:
        logger.error(f"处理插件文件夹时出错：{str(e)}")
        # 文件夹处理失败不影响插件分身创建的整体流程
