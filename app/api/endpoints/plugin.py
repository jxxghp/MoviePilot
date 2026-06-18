import mimetypes
import shutil
from typing import Annotated, Any, List, Optional

import aiofiles
from anyio import Path as AsyncPath
from fastapi import APIRouter, Depends, Header, HTTPException, Security
from fastapi.concurrency import run_in_threadpool
from starlette import status
from starlette.responses import StreamingResponse

from app import schemas
from app.command import Command
from app.core.cache import async_fresh
from app.core.config import settings
from app.core.event import eventmanager
from app.core.plugin import PluginManager
from app.core.security import (
    resource_token_cookie,
    verify_apikey,
    verify_resource_token,
    verify_token,
)
from app.db.models import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import (
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.factory import app
from app.helper.server import MoviePilotServerHelper
from app.helper.plugin import PluginHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType, SystemConfigKey

PROTECTED_ROUTES = {"/api/v1/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
PLUGIN_PREFIX = f"{settings.API_V1_STR}/plugin"

router = APIRouter()


def register_plugin_api(plugin_id: Optional[str] = None):
    """
    动态注册插件 API
    :param plugin_id: 插件 ID，如果为 None，则注册所有插件
    """
    _update_plugin_api_routes(plugin_id, action="add")


def remove_plugin_api(plugin_id: str):
    """
    动态移除单个插件的 API
    :param plugin_id: 插件 ID
    """
    _update_plugin_api_routes(plugin_id, action="remove")


def _update_plugin_api_routes(plugin_id: Optional[str], action: str):
    """
    插件 API 路由注册和移除
    :param plugin_id: 插件 ID，如果 action 为 "add" 且 plugin_id 为 None，则处理所有插件
                      如果 action 为 "remove"，plugin_id 必须是有效的插件 ID
    :param action: "add" 或 "remove"，决定是添加还是移除路由
    """
    if action not in {"add", "remove"}:
        raise ValueError("Action must be 'add' or 'remove'")

    is_modified = False
    existing_paths = {route.path: route for route in app.routes}

    plugin_ids = [plugin_id] if plugin_id else PluginManager().get_running_plugin_ids()
    for plugin_id in plugin_ids:
        routes_removed = _remove_routes(plugin_id)
        if routes_removed:
            is_modified = True

        if action != "add":
            continue
        # 获取插件的 API 路由信息
        plugin_apis = PluginManager().get_plugin_apis(plugin_id)
        for api in plugin_apis:
            api_path = f"{PLUGIN_PREFIX}{api.get('path', '')}"
            try:
                api["path"] = api_path
                allow_anonymous = api.pop("allow_anonymous", False)
                auth_mode = api.pop("auth", "apikey")
                dependencies = api.setdefault("dependencies", [])
                if not allow_anonymous:
                    if (
                        auth_mode == "bear"
                        and Depends(verify_token) not in dependencies
                    ):
                        dependencies.append(Depends(verify_token))
                    elif Depends(verify_apikey) not in dependencies:
                        dependencies.append(Depends(verify_apikey))
                app.add_api_route(**api, tags=["plugin"])
                is_modified = True
                logger.debug(f"Added plugin route: {api_path}")
            except Exception as e:
                logger.error(f"Error adding plugin route {api_path}: {str(e)}")

    if is_modified:
        _clean_protected_routes(existing_paths)
        app.openapi_schema = None
        app.setup()


def _remove_routes(plugin_id: str) -> bool:
    """
    移除与单个插件相关的路由
    :param plugin_id: 插件 ID
    :return: 是否有路由被移除
    """
    if not plugin_id:
        return False
    prefix = f"{PLUGIN_PREFIX}/{plugin_id}/"
    routes_to_remove = [route for route in app.routes if route.path.startswith(prefix)]
    removed = False
    for route in routes_to_remove:
        try:
            app.routes.remove(route)
            removed = True
            logger.debug(f"Removed plugin route: {route.path}")
        except Exception as e:
            logger.error(f"Error removing plugin route {route.path}: {str(e)}")
    return removed


def _clean_protected_routes(existing_paths: dict):
    """
    清理受保护的路由，防止在插件操作中被删除或重复添加
    :param existing_paths: 当前应用的路由路径映射
    """
    for protected_route in PROTECTED_ROUTES:
        try:
            existing_route = existing_paths.get(protected_route)
            if existing_route:
                app.routes.remove(existing_route)
        except Exception as e:
            logger.error(f"Error removing protected route {protected_route}: {str(e)}")


def register_plugin(plugin_id: str):
    """
    注册一个插件相关的服务
    """
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册菜单命令
    Command().init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)


def _merge_plugin_market_metadata(
    plugin: schemas.Plugin, market_plugin: schemas.Plugin
) -> schemas.Plugin:
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
) -> Optional[schemas.Plugin]:
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


@router.get("/", summary="所有插件", response_model=List[schemas.Plugin])
async def all_plugins(
    _: User = Depends(get_current_active_superuser_async),
    state: Optional[str] = "all",
    force: bool = False,
) -> List[schemas.Plugin]:
    """
    查询所有插件清单，包括本地插件和在线插件，插件状态：installed, market, all
    """
    # 本地插件
    plugin_manager = PluginManager()
    local_plugins = plugin_manager.get_local_plugins()
    # 已安装插件
    installed_plugins = [plugin for plugin in local_plugins if plugin.installed]
    if state == "installed":
        return installed_plugins

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
async def installed(_: User = Depends(get_current_active_superuser_async)) -> Any:
    """
    查询用户已安装插件清单
    """
    return SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []


@router.get("/history/{plugin_id}", summary="获取插件更新说明", response_model=schemas.Plugin)
async def plugin_history(
    plugin_id: str,
    _: User = Depends(get_current_active_superuser_async),
    force: bool = True,
) -> schemas.Plugin:
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


@router.get("/releases/{plugin_id}", summary="获取插件Release版本", response_model=dict)
async def plugin_releases(
    plugin_id: str,
    _: User = Depends(get_current_active_superuser_async),
    repo_url: Optional[str] = "",
    force: bool = False,
) -> dict:
    """
    查询指定插件可直接安装的 GitHub Release 版本。
    """
    if not repo_url:
        return {
            "release_supported": False,
            "latest_version": None,
            "current_version": None,
            "items": [],
        }

    plugin_manager = PluginManager()
    online_plugins = await plugin_manager.async_get_online_plugins(force=force)
    market_plugin = next(
        (
            plugin
            for plugin in online_plugins
            if plugin.id == plugin_id and plugin.repo_url == repo_url
        ),
        None,
    )
    if not market_plugin:
        market_plugin = next(
            (
                plugin
                for plugin in online_plugins
                if plugin.id == plugin_id
            ),
            None,
        )

    installed_plugin = next(
        (
            plugin
            for plugin in plugin_manager.get_local_plugins()
            if plugin.id == plugin_id and plugin.installed
        ),
        None,
    )
    latest_version = market_plugin.plugin_version if market_plugin else None
    current_version = installed_plugin.plugin_version if installed_plugin else None
    if not getattr(market_plugin, "release", False):
        return {
            "release_supported": False,
            "latest_version": latest_version,
            "current_version": current_version,
            "items": [],
        }

    async with async_fresh(force):
        release_items = await PluginHelper().async_get_plugin_release_versions(plugin_id, repo_url)
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


@router.get("/statistic", summary="插件安装统计", response_model=dict)
async def statistic(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    插件安装统计
    """
    return await MoviePilotServerHelper.async_get_plugin_statistic()


@router.get(
    "/reload/{plugin_id}", summary="重新加载插件", response_model=schemas.Response
)
def reload_plugin(
    plugin_id: str, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    重新加载插件
    """
    # 重新加载插件
    PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    register_plugin(plugin_id)
    return schemas.Response(success=True)


@router.get("/install/{plugin_id}", summary="安装插件", response_model=schemas.Response)
async def install(
    plugin_id: str,
    repo_url: Optional[str] = "",
    release_version: Optional[str] = None,
    force: Optional[bool] = False,
    _: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    安装插件
    """
    # 已安装插件
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    # 首先检查插件是否已经存在，并且是否强制安装，否则只进行安装统计
    plugin_helper = PluginHelper()
    if not force and plugin_id in PluginManager().get_plugin_ids():
        if repo_url:
            compatible_message = await plugin_helper.async_get_plugin_system_version_check_message(
                plugin_id, repo_url
            )
            if compatible_message:
                return schemas.Response(success=False, message=compatible_message)
        await MoviePilotServerHelper.async_install_plugin_reg(plugin_id=plugin_id, repo_url=repo_url)
    else:
        # 插件不存在或需要强制安装，下载安装并注册插件
        if repo_url:
            state, msg = await plugin_helper.async_install(
                pid=plugin_id, repo_url=repo_url, release_version=release_version, force_install=force
            )
            # 安装失败则直接响应
            if not state:
                return schemas.Response(success=False, message=msg)
            await MoviePilotServerHelper.async_install_plugin_reg(plugin_id=plugin_id, repo_url=repo_url)
        else:
            # repo_url 为空时，也直接响应
            return schemas.Response(
                success=False, message="没有传入仓库地址，无法正确安装插件，请检查配置"
            )
    # 安装插件
    if plugin_id not in install_plugins:
        install_plugins.append(plugin_id)
        # 保存设置
        await SystemConfigOper().async_set(
            SystemConfigKey.UserInstalledPlugins, install_plugins
        )
    # 重新加载插件
    await run_in_threadpool(reload_plugin, plugin_id)
    return schemas.Response(success=True)


@router.get("/remotes", summary="获取插件联邦组件列表", response_model=List[dict])
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
    response_model=List[schemas.PluginSidebarNavItem],
)
def plugin_sidebar_nav(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    聚合已启用 Vue 插件声明的侧栏入口（get_sidebar_nav），供前端主界面侧栏展示。
    """
    return PluginManager().get_plugin_sidebar_nav()


@router.get("/form/{plugin_id}", summary="获取插件表单页面")
def plugin_form(
    plugin_id: str, _: User = Depends(get_current_active_superuser)
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


@router.get("/page/{plugin_id}", summary="获取插件数据页面")
def plugin_page(
    plugin_id: str, _: User = Depends(get_current_active_superuser)
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


@router.get("/dashboard/meta", summary="获取所有插件仪表板元信息")
def plugin_dashboard_meta(
    _: User = Depends(get_current_active_superuser),
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
    _: User = Depends(get_current_active_superuser),
) -> Optional[schemas.PluginDashboard]:
    """
    根据插件ID获取插件仪表板
    """
    return PluginManager().get_plugin_dashboard(plugin_id, key, user_agent)


@router.get("/dashboard/{plugin_id}", summary="获取插件仪表板配置")
def plugin_dashboard(
    plugin_id: str,
    user_agent: Annotated[str | None, Header()] = None,
    _: User = Depends(get_current_active_superuser),
) -> Optional[schemas.PluginDashboard]:
    """
    根据插件ID获取插件仪表板
    """
    return plugin_dashboard_by_key(plugin_id, "", user_agent)


@router.get(
    "/reset/{plugin_id}", summary="重置插件配置及数据", response_model=schemas.Response
)
def reset_plugin(
    plugin_id: str, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    根据插件ID重置插件配置及数据
    """
    plugin_manager = PluginManager()
    eventmanager.send_event(
        ChainEventType.PluginDataReset,
        PluginDataResetEventData(plugin_id=plugin_id, reset_config=True, reset_data=True),
    )
    # 事件处理器需要运行中插件完成补偿；补偿后先停止插件，避免删除数据时仍有任务读写旧状态。
    plugin_manager.stop(plugin_id)
    # 删除配置
    plugin_manager.delete_plugin_config(plugin_id)
    # 删除插件所有数据
    plugin_manager.delete_plugin_data(plugin_id)
    # 重新加载插件
    reload_plugin(plugin_id)
    return schemas.Response(success=True)


@router.get("/file/{plugin_id}/{filepath:path}", summary="获取插件静态文件")
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

    plugin_base_dir = (
        AsyncPath(settings.ROOT_PATH) / "app" / "plugins" / plugin_id.lower()
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


@router.get("/folders", summary="获取插件文件夹配置", response_model=dict)
async def get_plugin_folders(
    _: User = Depends(get_current_active_superuser_async),
) -> dict:
    """
    获取插件文件夹分组配置
    """
    try:
        result = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
        return result
    except Exception as e:
        logger.error(f"[文件夹API] 获取文件夹配置失败: {str(e)}")
        return {}


@router.post("/folders", summary="保存插件文件夹配置", response_model=schemas.Response)
async def save_plugin_folders(
    folders: dict, _: User = Depends(get_current_active_superuser_async)
) -> Any:
    """
    保存插件文件夹分组配置
    """
    try:
        SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(success=True)
    except Exception as e:
        logger.error(f"[文件夹API] 保存文件夹配置失败: {str(e)}")
        return schemas.Response(success=False, message=str(e))


@router.post(
    "/folders/{folder_name}", summary="创建插件文件夹", response_model=schemas.Response
)
async def create_plugin_folder(
    folder_name: str, _: User = Depends(get_current_active_superuser_async)
) -> Any:
    """
    创建新的插件文件夹
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    if folder_name not in folders:
        folders[folder_name] = []
        SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(
            success=True, message=f"文件夹 '{folder_name}' 创建成功"
        )
    else:
        return schemas.Response(success=False, message=f"文件夹 '{folder_name}' 已存在")


@router.delete(
    "/folders/{folder_name}", summary="删除插件文件夹", response_model=schemas.Response
)
async def delete_plugin_folder(
    folder_name: str, _: User = Depends(get_current_active_superuser_async)
) -> Any:
    """
    删除插件文件夹
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    if folder_name in folders:
        del folders[folder_name]
        await SystemConfigOper().async_set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(
            success=True, message=f"文件夹 '{folder_name}' 删除成功"
        )
    else:
        return schemas.Response(success=False, message=f"文件夹 '{folder_name}' 不存在")


@router.put(
    "/folders/{folder_name}/plugins",
    summary="更新文件夹中的插件",
    response_model=schemas.Response,
)
async def update_folder_plugins(
    folder_name: str,
    plugin_ids: List[str],
    _: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    更新指定文件夹中的插件列表
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    folders[folder_name] = plugin_ids
    await SystemConfigOper().async_set(SystemConfigKey.PluginFolders, folders)
    return schemas.Response(
        success=True, message=f"文件夹 '{folder_name}' 中的插件已更新"
    )


@router.post(
    "/clone/{plugin_id}", summary="创建插件分身", response_model=schemas.Response
)
def clone_plugin(
    plugin_id: str, clone_data: dict, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    创建插件分身
    """
    try:
        success, message = PluginManager().clone_plugin(
            plugin_id=plugin_id,
            suffix=clone_data.get("suffix", ""),
            name=clone_data.get("name", ""),
            description=clone_data.get("description", ""),
            version=clone_data.get("version", ""),
            icon=clone_data.get("icon", ""),
        )

        if success:
            # 注册插件服务
            reload_plugin(message)
            # 将分身插件添加到原插件所在的文件夹中
            _add_clone_to_plugin_folder(plugin_id, message)
            return schemas.Response(success=True, message="插件分身创建成功")
        else:
            return schemas.Response(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return schemas.Response(success=False, message=f"创建插件分身失败：{str(e)}")


@router.get("/{plugin_id}", summary="获取插件配置")
async def plugin_config(
    plugin_id: str, _: User = Depends(get_current_active_superuser_async)
) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_config(plugin_id)


@router.put("/{plugin_id}", summary="更新插件配置", response_model=schemas.Response)
def set_plugin_config(
    plugin_id: str, conf: dict, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    更新插件配置
    """
    plugin_manager = PluginManager()
    # 保存配置
    plugin_manager.save_plugin_config(plugin_id, conf)
    # 重新生效插件
    plugin_manager.init_plugin(plugin_id, conf)
    # 注册插件服务
    register_plugin(plugin_id)
    return schemas.Response(success=True)


@router.delete("/{plugin_id}", summary="卸载插件", response_model=schemas.Response)
def uninstall_plugin(
    plugin_id: str, _: User = Depends(get_current_active_superuser)
) -> Any:
    """
    卸载插件
    """
    config_oper = SystemConfigOper()
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
    Scheduler().remove_plugin_job(plugin_id)
    # 判断是否为分身
    plugin_manager = PluginManager()
    plugin_class = plugin_manager.plugins.get(plugin_id)
    if getattr(plugin_class, "is_clone", False):
        # 如果是分身插件，则删除分身数据和配置
        plugin_manager.delete_plugin_config(plugin_id)
        plugin_manager.delete_plugin_data(plugin_id)
        # 删除分身文件
        plugin_base_dir = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        if plugin_base_dir.exists():
            try:
                shutil.rmtree(plugin_base_dir)
                plugin_manager.plugins.pop(plugin_id, None)
            except Exception as e:
                logger.error(f"删除插件分身目录 {plugin_base_dir} 失败: {str(e)}")
    # 从插件文件夹中移除该插件
    _remove_plugin_from_folders(plugin_id)
    # 移除插件
    plugin_manager.remove_plugin(plugin_id)
    return schemas.Response(success=True)


def _add_clone_to_plugin_folder(original_plugin_id: str, clone_plugin_id: str):
    """
    将分身插件添加到原插件所在的文件夹中
    :param original_plugin_id: 原插件ID
    :param clone_plugin_id: 分身插件ID
    """
    try:
        config_oper = SystemConfigOper()
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


def _remove_plugin_from_folders(plugin_id: str):
    """
    从所有文件夹中移除指定的插件
    :param plugin_id: 要移除的插件ID
    """
    try:
        config_oper = SystemConfigOper()
        # 获取插件文件夹配置
        folders = config_oper.get(SystemConfigKey.PluginFolders) or {}

        # 标记是否有修改
        modified = False

        # 遍历所有文件夹，移除指定插件
        for folder_name, folder_data in folders.items():
            if isinstance(folder_data, dict) and "plugins" in folder_data:
                # 新格式：{"plugins": [...], "order": ..., "icon": ...}
                if plugin_id in folder_data["plugins"]:
                    folder_data["plugins"].remove(plugin_id)
                    logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                    modified = True
            elif isinstance(folder_data, list):
                # 旧格式：直接是插件列表
                if plugin_id in folder_data:
                    folder_data.remove(plugin_id)
                    logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                    modified = True

        # 如果有修改，保存更新后的文件夹配置
        if modified:
            config_oper.set(SystemConfigKey.PluginFolders, folders)
        else:
            logger.debug(f"插件 {plugin_id} 不在任何文件夹中，无需移除")

    except Exception as e:
        logger.error(f"从文件夹中移除插件时出错：{str(e)}")
        # 文件夹处理失败不影响插件卸载的整体流程
