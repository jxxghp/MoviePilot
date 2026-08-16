"""插件 API 动态路由服务。

把插件 API 的动态注册/移除从 HTTP 端点层下沉到 application 层：
FastAPI 实例由组合根（factory 创建应用后）注入，端点与 Agent 工具
统一经本模块操作路由，消除 api.endpoints 对 factory 的反向依赖。

依赖方向：

    api.endpoints.plugin / agent.tools -> application.plugins <- factory（注入实例）
"""

from typing import Optional

from fastapi import Depends, FastAPI

from app.application.security.access import verify_apikey, verify_token
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

PROTECTED_ROUTES = {
    "/api/v1/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}
PLUGIN_PREFIX = f"{settings.API_V1_STR}/plugin"

# FastAPI 应用实例：由 factory 在创建应用后调用 register_api_app 注入。
_api_app: Optional[FastAPI] = None


def register_api_app(api_app: FastAPI) -> None:
    """注入 FastAPI 应用实例（组合根在创建应用后调用）。"""
    global _api_app
    _api_app = api_app


def get_api_app() -> FastAPI:
    """返回已注入的 FastAPI 应用实例。"""
    if _api_app is None:
        raise RuntimeError("插件路由服务未初始化：请先调用 register_api_app 注入应用实例")
    return _api_app


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

    app = get_api_app()
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
    app = get_api_app()
    prefix = f"{PLUGIN_PREFIX}/{plugin_id}/"
    routes_to_remove = [
        route for route in app.routes if route.path.startswith(prefix)
    ]
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
    app = get_api_app()
    for protected_route in PROTECTED_ROUTES:
        try:
            existing_route = existing_paths.get(protected_route)
            if existing_route:
                app.routes.remove(existing_route)
        except Exception as e:
            logger.error(f"Error removing protected route {protected_route}: {str(e)}")


def remove_plugin_from_folders(plugin_id: str):
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
