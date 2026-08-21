"""HTTP route/status/latency 指标 ASGI 适配器。"""

from __future__ import annotations

import time
from typing import Any

from app.runtime.observability import record_metric
from starlette.routing import Match


class HttpMetricsMiddleware:
    """按路由模板、方法和状态码记录低基数 HTTP 时延。"""

    def __init__(self, app: Any) -> None:
        """保存下游 ASGI 应用。"""
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """只治理 HTTP scope，并在响应开始后读取路由模板。"""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        started_at = time.perf_counter()
        status = "500"

        async def send_with_metrics(message: dict) -> None:
            """捕获响应状态并原样转发 ASGI 消息。"""
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

    def _resolve_route_template(self, scope: dict) -> str:
        """遍历 ASGI wrapper 找到匹配路由模板，绝不回退到具体请求 path。"""
        route = scope.get("route")
        if getattr(route, "path", None):
            return route.path
        candidate = self._app
        while candidate is not None:
            for registered_route in getattr(candidate, "routes", ()):
                match, _ = registered_route.matches(scope)
                if match == Match.FULL:
                    return getattr(registered_route, "path", "unmatched")
            candidate = getattr(candidate, "app", None)
        return "unmatched"
