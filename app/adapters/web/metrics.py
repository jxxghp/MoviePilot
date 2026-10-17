"""
记录 HTTP 路由、状态码和响应耗时的 ASGI 适配器。
"""

from __future__ import annotations

import time

from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.runtime.observability import record_metric


class HttpMetricsMiddleware:
    """
    按路由模板、方法和状态码记录低基数 HTTP 时延。
    """

    def __init__(self, app: ASGIApp) -> None:
        """
        保存下游 ASGI 应用。
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        记录 HTTP 请求从开始处理到响应结束的耗时，其他连接类型直接转交下游。

        下游抛出异常时也记录指标；尚未收到响应头时按 500 状态记录。
        """
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        started_at = time.perf_counter()
        status = "500"

        async def send_with_metrics(message: Message) -> None:
            """
            捕获响应状态并原样转发 ASGI 消息。
            """
            nonlocal status
            if message.get("type") == "http.response.start":
                status = str(message.get("status", 500))
            await send(message)

        try:
            await self._app(scope, receive, send_with_metrics)
        finally:
            route_path = self._resolve_route_template(scope)
            record_metric(
                "http.server.duration",
                time.perf_counter() - started_at,
                route=route_path,
                method=str(scope.get("method", "UNKNOWN")),
                status=status,
            )

    def _resolve_route_template(self, scope: Scope) -> str:
        """
        逐层检查 ASGI 应用，找到匹配的路由模板。

        只记录模板，避免具体请求路径造成高基数标签；未匹配时返回 unmatched。
        """
        route = scope.get("route")
        if route is not None and getattr(route, "path", None):
            route_path: str = route.path
            return route_path
        candidate: object | None = self._app
        while candidate is not None:
            for registered_route in getattr(candidate, "routes", ()):
                match, _ = registered_route.matches(scope)
                if match == Match.FULL:
                    matched_path: str = getattr(registered_route, "path", "unmatched")
                    return matched_path
            candidate = getattr(candidate, "app", None)
        return "unmatched"
