"""FastAPI 动态插件路由适配器。"""

from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute


class FastAPIDynamicRouteRegistry:
    """在 FastAPI 上注册插件自由响应路由，并维护 OpenAPI 缓存。"""

    def __init__(
        self,
        app: FastAPI,
        plugin_ids: Callable[[], list[str]],
        plugin_apis: Callable[[str], list[dict]],
        verify_token: Callable[..., Any],
        verify_apikey: Callable[..., Any],
        prefix: str,
        protected_routes: set[str],
        log: Any,
        route_matches: Callable[[str, Optional[str]], bool],
    ) -> None:
        """注入应用、插件投影、认证依赖、日志端口和实例键匹配判据。

        :param route_matches: 判断路由路径首段（实例键）是否命中筛选条件的谓词，
            签名与 `app.runtime.extensions.contract.instance.matches_extension` 一致；
            适配器层不直接依赖插件运行时模块，由组合根注入具体实现
        """
        self._app = app
        self._plugin_ids = plugin_ids
        self._plugin_apis = plugin_apis
        self._verify_token = verify_token
        self._verify_apikey = verify_apikey
        self._prefix = prefix
        self._protected_routes = protected_routes
        self._logger = log
        self._route_matches = route_matches

    def update(self, plugin_id: Optional[str], action: str) -> None:
        """按插件生命周期新增或移除动态路由。"""
        if action not in {"add", "remove"}:
            raise ValueError("Action must be 'add' or 'remove'")

        modified = False
        existing_paths = {
            path: route
            for route in self._app.routes
            if (path := self._route_path(route)) is not None
        }
        plugin_ids = [plugin_id] if plugin_id else self._plugin_ids()
        for current_id in plugin_ids:
            if self.remove(current_id):
                modified = True
            if action != "add":
                continue
            for source_api in self._plugin_apis(current_id):
                api = dict(source_api)
                api["dependencies"] = list(source_api.get("dependencies") or ())
                api_path = f"{self._prefix}{api.get('path', '')}"
                try:
                    api["path"] = api_path
                    allow_anonymous = api.pop("allow_anonymous", False)
                    auth_mode = api.pop("auth", "apikey")
                    dependencies = api.setdefault("dependencies", [])
                    if not allow_anonymous:
                        if (
                            auth_mode == "bear"
                            and Depends(self._verify_token) not in dependencies
                        ):
                            dependencies.append(Depends(self._verify_token))
                        elif Depends(self._verify_apikey) not in dependencies:
                            dependencies.append(Depends(self._verify_apikey))
                    # 插件 API 自行决定响应结构，不使用宿主统一 Response 路由。
                    api.setdefault("route_class_override", APIRoute)
                    self._app.router.add_api_route(**api, tags=["plugin"])
                    modified = True
                    self._logger.debug(f"Added plugin route: {api_path}")
                except Exception as error:
                    self._logger.error(
                        f"Error adding plugin route {api_path}: {str(error)}"
                    )
        if modified:
            self.clean(existing_paths)
            self._app.openapi_schema = None
            self._app.setup()

    def remove(self, plugin_id: str) -> bool:
        """移除指定插件标识或实例键命中的全部动态路由。

        路由路径首段即插件实例的实例键：传插件标识命中该插件全部实例的路由，
        避免删整个插件时留下分身路由；传实例键只精确命中该实例，不误删兄弟
        实例的路由。

        :param plugin_id: 插件标识或实例键
        :return: 是否有路由被移除
        """
        if not plugin_id:
            return False
        base = f"{self._prefix}/"
        routes = []
        for route in self._app.routes:
            path = self._route_path(route)
            if path is None or not path.startswith(base):
                continue
            segment = path[len(base):].split("/", 1)[0]
            if self._route_matches(segment, plugin_id):
                routes.append(route)
        removed = False
        for route in routes:
            try:
                self._app.routes.remove(route)
                removed = True
                self._logger.debug(f"Removed plugin route: {self._route_path(route)}")
            except Exception as error:
                self._logger.error(
                    f"Error removing plugin route {self._route_path(route)}: {str(error)}"
                )
        return removed

    @staticmethod
    def _route_path(route: Any) -> Optional[str]:
        """返回公开路由路径，跳过 FastAPI 内部的无路径 include 包装器。"""
        path = getattr(route, "path", None)
        return path if isinstance(path, str) else None

    def clean(self, existing_paths: dict) -> None:
        """清理 FastAPI 重建时可能重复的受保护文档路由。"""
        for protected_route in self._protected_routes:
            try:
                existing_route = existing_paths.get(protected_route)
                if existing_route:
                    self._app.routes.remove(existing_route)
            except Exception as error:
                self._logger.error(
                    f"Error removing protected route {protected_route}: {str(error)}"
                )
