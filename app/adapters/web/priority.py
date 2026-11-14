"""
为宿主 Agent 的本机 HTTP 调用恢复临时执行归属。
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from app.runtime.priority import PRIORITY_HEADER, propagated_execution


class ExecutionPriorityMiddleware:
    """
    仅凭本进程登记的临时随机凭据恢复优先级，正常鉴权仍由下游负责。
    """

    def __init__(self, app: ASGIApp) -> None:
        """
        保存要恢复执行上下文的下游应用。
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        覆盖完整 HTTP 响应生命周期，其他连接类型直接转交下游。
        """
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        key = headers.get(PRIORITY_HEADER.lower().encode("ascii"), b"")
        with propagated_execution(key.decode("ascii", errors="ignore")):
            await self._app(scope, receive, send)
