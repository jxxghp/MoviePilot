"""FastAPI 动态插件路由适配器。"""

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from threading import Lock
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute


class FastAPIDynamicRouteRegistry:
    """在 FastAPI 上注册插件自由响应路由，并维护 OpenAPI 缓存。"""

    _dispatch_admission_timeout = 5.0

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
        event_loop: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
    ) -> None:
        """注入应用、插件投影、认证依赖和日志端口。"""
        self._app = app
        self._plugin_ids = plugin_ids
        self._plugin_apis = plugin_apis
        self._verify_token = verify_token
        self._verify_apikey = verify_apikey
        self._prefix = prefix
        self._protected_routes = protected_routes
        self._logger = log
        self._event_loop = event_loop

    def update(self, plugin_id: Optional[str], action: str) -> None:
        """在主事件循环中按插件生命周期新增或移除动态路由。"""
        if self._event_loop is None:
            self._update(plugin_id, action)
            return
        target_loop = self._event_loop()
        if (
            target_loop is None
            or not target_loop.is_running()
            or target_loop.is_closed()
        ):
            raise RuntimeError("主事件循环未运行，无法更新插件动态路由")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is target_loop:
            self._update(plugin_id, action)
            return

        completed: Future[None] = Future()
        dispatch_lock = Lock()
        dispatch_started = False
        dispatch_abandoned = False

        def apply_update() -> None:
            """在目标 loop 的单个回调中完成路由表与 OpenAPI 投影切换。"""
            nonlocal dispatch_started
            with dispatch_lock:
                if dispatch_abandoned:
                    return
                dispatch_started = True
            try:
                self._update(plugin_id, action)
            except BaseException as error:
                completed.set_exception(error)
            else:
                completed.set_result(None)

        target_loop.call_soon_threadsafe(apply_update)
        try:
            completed.result(timeout=self._dispatch_admission_timeout)
        except FutureTimeoutError as error:
            with dispatch_lock:
                if not dispatch_started:
                    dispatch_abandoned = True
                    raise RuntimeError(
                        "主事件循环未及时接收插件动态路由更新"
                    ) from error
            # 回调一旦开始便不可撤销，等待确定终态以免失败回滚后发生迟到写入。
            completed.result()

    def _update(self, plugin_id: Optional[str], action: str) -> None:
        """执行不可中断的路由表与 OpenAPI 投影更新。"""
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
        """移除指定插件前缀下的全部动态路由。"""
        if not plugin_id:
            return False
        prefix = f"{self._prefix}/{plugin_id}/"
        routes = [
            route for route in self._app.routes
            if (path := self._route_path(route)) is not None
            and path.startswith(prefix)
        ]
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
