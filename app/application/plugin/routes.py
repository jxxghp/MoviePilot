"""动态插件路由应用端口与注册用例。"""

from typing import Optional, Protocol


class DynamicRouteRegistry(Protocol):
    """插件生命周期更新动态 HTTP 路由所需的最小端口。"""

    def update(self, plugin_id: Optional[str], action: str) -> None:
        """新增或移除指定插件的动态路由。"""
        ...


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
    """动态注册插件 API。"""
    _update_plugin_api_routes(plugin_id, action="add")


def remove_plugin_api(plugin_id: str) -> None:
    """动态移除单个插件的 API。"""
    _update_plugin_api_routes(plugin_id, action="remove")


def _update_plugin_api_routes(plugin_id: Optional[str], action: str) -> None:
    """
    更新插件动态路由。

    :param plugin_id: 插件 ID；注册时为空表示处理全部插件
    :param action: ``add`` 或 ``remove``
    """
    _get_route_registry().update(plugin_id, action)
