"""插件 API 动态路由服务。

把插件 API 的动态注册/移除从 HTTP 端点层下沉到 application 层：
FastAPI 实例由组合根（factory 创建应用后）注入，端点与 Agent 工具
统一经本模块操作路由，消除 api.endpoints 对 factory 的反向依赖。

依赖方向：

    api.endpoints.plugin / agent.tools -> application.plugins <- factory（注入实例）
"""

from typing import Optional

from app.application.plugin.routes import DynamicRouteRegistry
from app.application.configuration import get_configured_system_config
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

_route_registry: Optional[DynamicRouteRegistry] = None


def configure_plugin_routes(registry: DynamicRouteRegistry) -> None:
    """由 HTTP 组合根注入动态插件路由适配器。"""
    global _route_registry
    _route_registry = registry


def _get_route_registry() -> DynamicRouteRegistry:
    """返回已注入的动态插件路由端口。"""
    if _route_registry is None:
        raise RuntimeError("插件路由服务尚未由 HTTP 组合根配置")
    return _route_registry


def register_plugin_api(plugin_id: Optional[str] = None) -> None:
    """
    动态注册插件 API
    :param plugin_id: 插件 ID，如果为 None，则注册所有插件
    """
    _update_plugin_api_routes(plugin_id, action="add")


def remove_plugin_api(plugin_id: str) -> None:
    """
    动态移除单个插件的 API
    :param plugin_id: 插件 ID
    """
    _update_plugin_api_routes(plugin_id, action="remove")


def _update_plugin_api_routes(plugin_id: Optional[str], action: str) -> None:
    """
    插件 API 路由注册和移除
    :param plugin_id: 插件 ID，如果 action 为 "add" 且 plugin_id 为 None，则处理所有插件
                      如果 action 为 "remove"，plugin_id 必须是有效的插件 ID
    :param action: "add" 或 "remove"，决定是添加还是移除路由
    """
    _get_route_registry().update(plugin_id, action)


def _remove_routes(plugin_id: str) -> bool:
    """
    移除与单个插件相关的路由
    :param plugin_id: 插件 ID
    :return: 是否有路由被移除
    """
    return _get_route_registry().remove(plugin_id)


def _clean_protected_routes(existing_paths: dict) -> None:
    """
    清理受保护的路由，防止在插件操作中被删除或重复添加
    :param existing_paths: 当前应用的路由路径映射
    """
    _get_route_registry().clean(existing_paths)


def remove_plugin_from_folders(plugin_id: str):
    """
    从所有文件夹中移除指定的插件
    :param plugin_id: 要移除的插件ID
    """
    try:
        config_oper = get_configured_system_config()
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
