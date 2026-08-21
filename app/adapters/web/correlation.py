"""HTTP 请求关联 ID 的 ASGI 适配器。"""

from __future__ import annotations

from typing import Any

from app.runtime.correlation import (
    CORRELATION_ID_HEADER,
    correlation_scope,
    normalize_correlation_id,
)


class CorrelationIdMiddleware:
    """验证入口 ID、绑定请求上下文并把同一 ID 写回响应。"""

    def __init__(self, app: Any) -> None:
        """保存下游 ASGI 应用。"""
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """只治理 HTTP scope，并让绑定覆盖完整流式响应生命周期。"""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        raw_headers = dict(scope.get("headers") or [])
        candidate = raw_headers.get(CORRELATION_ID_HEADER.lower().encode("ascii"))
        correlation_id = normalize_correlation_id(
            candidate.decode("ascii", errors="ignore") if candidate else None
        )
        scope.setdefault("state", {})["request_id"] = correlation_id

        async def send_with_correlation(message: dict) -> None:
            """在响应开始帧中覆盖为当前请求的安全关联 ID。"""
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                header_name = CORRELATION_ID_HEADER.lower().encode("ascii")
                headers = [item for item in headers if item[0].lower() != header_name]
                headers.append((header_name, correlation_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        with correlation_scope(correlation_id):
            await self._app(scope, receive, send_with_correlation)
