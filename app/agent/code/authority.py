"""每个 Python cell 独立的宿主身份、只读工具集合和调用预算。"""

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import (
    ActionEffect,
    ExecutionOutcome,
    PrincipalRole,
    ResultSensitivity,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.registry import requests_system_setting_secrets
from app.agent.policy.sanitizer import summarize_error, summarize_input, summarize_result
from app.agent.terminal.ownership import TerminalScope
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.search_web import SearchWebTool
from app.agent.tools.result import inspect_tool_result
from app.runtime.tasks import get_task_registry

MAX_TOOL_CALLS = 50
RPC_RESULT_KEY = '_moviepilot_code_result'
CURRENT_CELL: contextvars.ContextVar['CellAuthority | None'] = contextvars.ContextVar('agent_code_cell', default=None)
_READ_CLASSES = (MoviePilotApiTool, ReadFileTool, SearchWebTool)
Dispatch = Callable[[Any, dict[str, Any]], Awaitable[Any]]


def allowed_tools(tools: list[Any]) -> dict[str, Any]:
    """只取本轮实际启用的原生工具对象，插件或 MCP 同名工具不能借用只读声明。"""
    return {tool.name: tool for tool in tools if type(tool) in _READ_CLASSES
            and getattr(tool, '_agent_tool_source', None) == 'builtin'}


class CellAuthority:
    """当前 cell 的调用窗口；持久 socket 和 Python 状态不延长宿主调用身份。"""

    def __init__(self, scope: TerminalScope, context: ToolPolicyContext,
                 tools: dict[str, Any], dispatch: Dispatch) -> None:
        """从正在执行的宿主协程复制上下文，每次 cell 都重新建立，结束即撤销。"""
        self.scope, self.context, self.tools = scope, context, tools
        self._dispatch = dispatch
        self._context = contextvars.copy_context()
        self._active = True
        self._pending: set[asyncio.Task[Any]] = set()
        self.calls = 0
        self.log: list[dict[str, Any]] = []
        self.errors: list[dict[str, str]] = []

    def available(self) -> bool:
        """角色引用随宿主更新而变化，排队或持久连接不得继续使用已撤销权限。"""
        return (self._active and not self.scope.closed and bool(self.scope.user_id)
                and self.scope.user_id == self.context.user_id
                and self.context.principal.role is PrincipalRole.SYSTEM_ADMIN
                and self.context.origin not in {ToolOrigin.SUBAGENT, ToolOrigin.AGENT_API})

    def _error(self, name: str, reason: str) -> dict[str, Any]:
        """即使脚本忽略失败回执，外层结果仍保留最多五条明确错误。"""
        if len(self.errors) < 5:
            self.errors.append({'tool': name, 'error': reason})
        return {'success': False, 'error': reason}

    @staticmethod
    def _read_allowed(tool: Any, arguments: dict[str, Any]) -> bool:
        """API 的操作副作用与敏感参数都由宿主合同解析，不采用模型声明的读取标签。"""
        if type(tool) is not MoviePilotApiTool:
            return type(tool) in {ReadFileTool, SearchWebTool}
        operation = resolve_api_operation(str(arguments.get('operation_id') or ''))
        if (operation is None or operation.effect is not ActionEffect.SAFE_READ
                or operation.result_sensitivity not in {ResultSensitivity.NORMAL, ResultSensitivity.PRIVATE}):
            return False
        normalized = tool.canonical_arguments(arguments)
        return not requests_system_setting_secrets(normalized)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """逐次检查、计数并在当前 cell 上下文执行，串行 RPC 不会超发预算。"""
        if not self.available():
            return self._error(name, '当前 Python 调用窗口已结束或用户权限已变化。')
        tool = self.tools.get(name)
        if tool is None:
            return self._error(name, '当前 cell 未启用此原生只读工具。')
        if self.calls >= MAX_TOOL_CALLS:
            return self._error(name, f'已达到单次代码执行的 {MAX_TOOL_CALLS} 次工具调用上限。')
        self.calls += 1
        started = time.monotonic()
        try:
            if not self._read_allowed(tool, arguments):
                result = self._error(name, '代码内只允许安全只读操作，写入、凭据与未知敏感级别的读取不可调用。')
            else:
                task = self._context.run(get_task_registry().create, self._invoke(tool, arguments), owner='agent.code.dispatch')
                self._pending.add(task)
                try:
                    result = await task
                finally:
                    self._pending.discard(task)
        except (TypeError, ValueError) as error:
            result = self._error(name, summarize_error(error))
        except asyncio.CancelledError:
            self._record(name, arguments, started, ExecutionOutcome.UNKNOWN)
            raise
        self._record(name, arguments, started, inspect_tool_result(result))
        return result

    def _record(self, name: str, arguments: dict[str, Any], started: float, outcome: ExecutionOutcome) -> None:
        """每次实际准入调用都有脱敏回执，中断也不能从调用记录中消失。"""
        self.log.append({'tool': name, 'args_preview': summarize_input(arguments, max_chars=80),
                         'duration': round(time.monotonic() - started, 2), 'outcome': outcome.value})

    async def _invoke(self, tool: Any, arguments: dict[str, Any]) -> Any:
        """重新检查排队期间变化的身份；工具异常只产生脱敏回执，取消保持传播。"""
        if not self.available():
            return self._error(tool.name, '当前 Python 调用窗口已结束或用户权限已变化。')
        try:
            result = await self._dispatch(tool, arguments)
        except Exception as error:
            return self._error(tool.name, summarize_error(error))
        if inspect_tool_result(result) is not ExecutionOutcome.SUCCEEDED:
            self._error(tool.name, summarize_result(result, max_chars=300))
        return result

    async def retire(self) -> None:
        """先同步封口，再取消 cell 遗留调用并等待真实结算。"""
        self._active = False
        pending = [task for task in self._pending if not task.done()]
        if pending:
            self._error('execute_code', f'cell 结束时仍有 {len(pending)} 次工具调用未完成，已取消。')
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
