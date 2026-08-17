"""动态插件路由应用端口。"""

from typing import Optional, Protocol


class DynamicRouteRegistry(Protocol):
    """插件生命周期操作动态 HTTP 路由所需的最小端口。"""

    def update(self, plugin_id: Optional[str], action: str) -> None:
        """新增或移除指定插件的动态路由。"""
        ...

    def remove(self, plugin_id: str) -> bool:
        """移除指定插件的全部动态路由。"""
        ...
