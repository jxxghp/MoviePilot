"""将 Python 子进程的逐次调用接回当前 Agent 的真实工具链。"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.code.authority import CURRENT_CELL, RPC_RESULT_KEY, CellAuthority, allowed_tools
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import ExecutionOutcome
from app.agent.terminal.ownership import TerminalScope, current_terminal_scope
from app.agent.tools.impl.execute_code import ExecuteCodeTool
from app.agent.tools.result import inspect_tool_result


class CodeExecutionMiddleware(AgentMiddleware):  # type: ignore[misc]
    """外层绑定执行窗口；内层调用保留策略、Skill、日志和原始结果归档行为。"""

    def __init__(self, policy: AgentPolicyMiddleware) -> None:
        """共享同一个宿主策略实例，不复制权限或跳过当前身份事实源。"""
        self.policy = policy
        self.selections: dict[TerminalScope, dict[str, dict[str, Any]]] = {}

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]) -> Any:
        """只为模型真实请求中已启用的原生 execute_code 绑定当前窗口，默认直接执行。"""
        if type(request.tool) is not ExecuteCodeTool:
            return await handler(request)
        scope = current_terminal_scope()
        selections = self.selections.get(scope, {}) if scope is not None else {}
        tools = selections.pop(str(request.tool_call['id']), None)
        if scope is None or tools is None:
            return ToolMessage(content='当前 Python 请求缺少宿主工具快照，未执行代码。',
                               name='execute_code', tool_call_id=request.tool_call['id'], status='error')

        async def dispatch(tool: Any, arguments: dict[str, Any]) -> Any:
            """重入当前 handler，逐次经过 Skill 和工具门禁，而不是直接调用工具方法。"""
            return await self._dispatch(request, handler, tool, arguments)

        authority = CellAuthority(scope, self.policy.context, tools, dispatch)
        token = CURRENT_CELL.set(authority)
        try:
            return await handler(request)
        finally:
            CURRENT_CELL.reset(token)
            await authority.retire()
            if not selections:
                self.selections.pop(scope, None)

    async def _dispatch(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]],
                        tool: Any, arguments: dict[str, Any]) -> Any:
        """每次内部调用使用独立回执 ID；原文只交给 Python，外层模型只接收程序输出。"""
        identifier = uuid.uuid4().hex
        raw: dict[str, str] = {}
        state = {**request.state, RPC_RESULT_KEY: raw}
        runtime = replace(request.runtime, tool_call_id=identifier, state=state)
        nested = ToolCallRequest(tool=tool, tool_call={'name': tool.name, 'args': arguments, 'id': identifier, 'type': 'tool_call'},
                                 state=state, runtime=runtime)
        allowed, result = await self.policy.execute_tool_call(
            tool=tool, arguments=arguments, invocation_id=identifier,
            handler=lambda: handler(nested), enforce_decision=True,
        )
        if not allowed:
            return {'success': False, 'error': str(result)}
        if not isinstance(result, ToolMessage):
            return {'success': False, 'error': '代码内调用未返回可读取的工具回执。'}
        content = raw.get(tool.name, result.content)
        if not isinstance(content, str):
            return {'success': False, 'error': '代码内调用只支持文本或 JSON 结果。'}
        if len(content.encode('utf-8')) > 16 * 1024 * 1024:
            return {'success': False, 'error': '单次工具结果超过16MB，请使用工具的分页或过滤参数缩小结果。'}
        try:
            payload = json.loads(content)
        except ValueError:
            payload = content
        if inspect_tool_result(result) is not ExecutionOutcome.SUCCEEDED:
            return {'success': False, 'error': payload}
        return payload


class CodeCaptureMiddleware(AgentMiddleware):  # type: ignore[misc]
    """筛选后、压缩前生成真实 helper 合同，新增说明必须计入最终请求预算。"""

    def __init__(self, execution: CodeExecutionMiddleware) -> None:
        """捕获器与工具包装器共享短生命周期快照，不增加图节点或注册重复工具。"""
        self.execution = execution

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """只为本次模型实际发出的代码调用保存快照，下一次请求替换同 owner 的旧快照。"""
        scope = current_terminal_scope()
        if scope is not None:
            self.execution.selections.pop(scope, None)
        tools = allowed_tools(request.tools)
        signatures = []
        for name, tool in tools.items():
            schema = tool.args_schema.model_json_schema()
            parameters = [field if field in schema.get('required', []) else f'{field}=...'
                          for field in schema.get('properties', {})]
            signatures.append(f'{name}({", ".join(parameters)})')
        description = '\nCurrent Python helpers: ' + ('; '.join(signatures) or 'none; only local Python computation is available.')
        request = request.override(tools=[tool.model_copy(update={'description': tool.description + description})
                                         if type(tool) is ExecuteCodeTool else tool for tool in request.tools])
        result = await handler(request)
        if scope is None or not any(type(tool) is ExecuteCodeTool for tool in request.tools):
            return result
        selections = {call['id']: tools for message in result.result if isinstance(message, AIMessage)
                      for call in message.tool_calls if call['name'] == 'execute_code'}
        if selections:
            self.execution.selections[scope] = selections
        return result
