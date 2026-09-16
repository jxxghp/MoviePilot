"""LangChain 工具调用的 MoviePilot 宿主策略中间件。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import (
    ActionEffect,
    ExecutionOutcome,
    ExecutionReceipt,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.orchestrator import (
    DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    AgentToolPolicyOrchestrator,
    call_policy_hook,
)
from app.agent.policy.registry import requests_system_setting_secrets
from app.agent.policy.sanitizer import stable_type_name
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool
from app.agent.tools.result import EXECUTION_OUTCOME_KEY, ToolExecutionError, annotate_tool_result
from app.agent.tools.tags import ToolTag

POLICY_DENIED_MESSAGE = "当前宿主策略不允许执行该工具。"
POLICY_UNAVAILABLE_MESSAGE = "宿主策略暂时不可用，未执行该工具。"
TOOL_TIMEOUT_MESSAGE = "工具执行超时，已停止等待结果；若工具包含外部写操作，操作可能仍在继续，请先确认实际状态再重试。"
SUBAGENT_READ_ACTIONS = {
    "execute_command": frozenset({"read", "wait"}),
    "browse_webpage": frozenset({"snapshot", "get_content", "screenshot", "wait", "list_tabs"}),
    "persona": frozenset({"list"}),
    "agent_task": frozenset({"list"}),
}


# follow_imports=skip 下只在第三方中间件基类和装饰器边界忽略 misc。
class AgentPolicyMiddleware(AgentMiddleware):  # type: ignore[misc]
    """观测进入本地 ToolNode 的 client-side 工具调用和结果。

    模型供应商原生 server tools 在供应商侧执行，不经过本地 middleware，
    因而不具备这里生成的 start/finish/fail 回执。
    只读子代理的动作范围由宿主强制执行，不受主代理兼容观测模式影响。
    """

    def __init__(
        self,
        *,
        context: ToolPolicyContext,
        orchestrator: AgentToolPolicyOrchestrator = DEFAULT_TOOL_POLICY_ORCHESTRATOR,
        catalog: ToolCatalogSnapshot | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        """绑定宿主可信上下文和共享策略编排器。"""
        self.context = context
        self.orchestrator = orchestrator
        self.catalog = catalog
        self._tools = {tool.name: tool for tool in (tools or []) if getattr(tool, "name", None)}

    @hook_config(can_jump_to=["end"])  # type: ignore[misc]
    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> Any:
        """在 ToolNode 前暂停需要用户确认的敏感设置读取。"""
        messages = state.get("messages") or []
        if not messages or not isinstance(messages[-1], AIMessage):
            return None

        tool_calls = messages[-1].tool_calls or []
        sensitive_call = None
        sensitive_tool = None
        for tool_call in tool_calls:
            arguments = tool_call.get("args")
            tool = self._tools.get(tool_call.get("name"))
            if (
                isinstance(tool, MoviePilotApiTool)
                and isinstance(arguments, dict)
                and requests_system_setting_secrets(arguments)
            ):
                sensitive_call = tool_call
                sensitive_tool = tool
                break
        if sensitive_call is None or sensitive_tool is None:
            return None

        confirmation_handler = (
            self.context.agent_context.get("secret_confirmation_handler")
            if self.context.origin is ToolOrigin.AGENT_INTERACTIVE
            else None
        )
        if not callable(confirmation_handler):
            confirmation_message = "当前入口不支持敏感设置确认，未执行任何工具。"
        else:
            confirmation_message = await confirmation_handler(
                sensitive_tool,
                sensitive_call.get("args") or {},
            )

        paused_messages = [
            ToolMessage(
                content=("本轮工具调用已暂停，未执行任何操作；请等待用户确认或取消敏感设置读取。"),
                tool_call_id=str(tool_call.get("id") or ""),
                name=str(tool_call.get("name") or "unknown"),
            )
            for tool_call in tool_calls
        ]
        paused_messages.append(AIMessage(content=confirmation_message))
        return {"messages": paused_messages, "jump_to": "end"}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在 handler 外层生成 shadow 决策和 secret-safe 回执摘要。"""
        tool_call = request.tool_call or {}
        arguments = tool_call.get("args") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        _, result = await self.execute_tool_call(
            tool=request.tool,
            arguments=arguments,
            invocation_id=tool_call.get("id"),
            handler=lambda: handler(request),
            enforce_decision=False,
        )
        # 普通 ToolNode 保持 shadow 观测；已确认调用使用默认的强制决策语义。
        return annotate_tool_result(result)

    @staticmethod
    def _error_result(
        tool: Any, invocation_id: str | None, error: Exception, receipt: ExecutionReceipt | None,
    ) -> ToolMessage:
        """把普通故障转为可恢复回执，异常私有正文不进入模型上下文。"""
        outcome = receipt.outcome if isinstance(receipt, ExecutionReceipt) else ExecutionOutcome.FAILED
        if isinstance(error, TimeoutError):
            content = TOOL_TIMEOUT_MESSAGE
            if not isinstance(receipt, ExecutionReceipt):
                outcome = ExecutionOutcome.UNKNOWN
        elif isinstance(error, ToolExecutionError):
            content = str(error)
        else:
            content = f"工具执行失败（{stable_type_name(error)}）。请检查调用参数或查询当前状态后继续处理。"
        return ToolMessage(
            content=content,
            tool_call_id=str(invocation_id or ""),
            name=str(getattr(tool, "name", None) or "unknown"),
            status="error",
            additional_kwargs={EXECUTION_OUTCOME_KEY: outcome.value},
        )

    @staticmethod
    def _subagent_read_only_error(tool: Any, arguments: dict[str, Any], invocation_id: str | None) -> ToolMessage:
        """为子代理的只读拒绝返回可纠正的 operation 合同，而不是只给笼统权限错误。"""
        payload: dict[str, Any] = {
            "success": False,
            "error": "subagent_read_only",
            "message": "子代理只允许已声明的安全只读操作；请由主代理核验并执行写入。",
        }
        if isinstance(tool, MoviePilotApiTool):
            operation_id = str(arguments.get("operation_id") or "")
            operation = resolve_api_operation(operation_id)
            if operation is not None and operation.effect is ActionEffect.SAFE_READ:
                try:
                    tool.canonical_arguments(arguments)
                except (TypeError, ValueError) as error:
                    payload["message"] = (
                        f"{operation_id} 是安全只读操作，但输入未通过当前合同（{str(error)}）。"
                        "请按 input_contract 只提交允许字段并补齐 required 字段后重试。"
                    )
                    payload["operation_id"] = operation_id
                    payload["input_contract"] = tool.get_operation_input_contract(operation_id)
            else:
                payload["message"] = (
                    f"{operation_id or '当前 operation'} 不是子代理可执行的安全只读操作。"
                    "请改用明确的只读 operation；写入、删除、刷新和敏感读取必须由主代理处理。"
                )
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=str(invocation_id or ""),
            name=str(getattr(tool, "name", None) or "unknown"),
            status="error",
            additional_kwargs={EXECUTION_OUTCOME_KEY: ExecutionOutcome.FAILED.value},
        )

    async def execute_tool_call(
        self,
        *,
        tool: Any,
        arguments: dict[str, Any],
        handler: Callable[[], Awaitable[Any]],
        invocation_id: str | None = None,
        enforce_decision: bool = True,
    ) -> tuple[bool, Any]:
        """执行一次本地工具调用，并复用 ToolNode 的策略生命周期。"""
        if self.context.origin is ToolOrigin.SUBAGENT and not self._subagent_read_allowed(tool, arguments):
            return False, self._subagent_read_only_error(tool, arguments, invocation_id)
        observation = call_policy_hook(
            "start",
            self.orchestrator.start,
            context=self.context,
            tool=tool,
            arguments=arguments,
            invocation_id=invocation_id,
        )
        if enforce_decision and observation is None:
            return False, POLICY_UNAVAILABLE_MESSAGE
        if enforce_decision and observation is not None and observation.decision.allowed is False:
            return False, POLICY_DENIED_MESSAGE
        try:
            result = await handler()
        except asyncio.CancelledError as error:
            if observation is not None:
                call_policy_hook(
                    "cancel",
                    self.orchestrator.fail,
                    observation,
                    error,
                )
            raise
        except Exception as error:
            receipt = None
            if observation is not None:
                receipt = call_policy_hook(
                    "fail",
                    self.orchestrator.fail,
                    observation,
                    error,
                )
            if not enforce_decision:
                return True, self._error_result(tool, invocation_id, error, receipt)
            raise
        if observation is not None:
            call_policy_hook(
                "finish",
                self.orchestrator.finish,
                observation,
                result,
            )
        return True, result

    @staticmethod
    def _subagent_read_allowed(tool: Any, arguments: dict[str, Any]) -> bool:
        """按真实 API 操作限制子代理，MCP 的兼容 Read 标签不构成只读证明。"""
        if isinstance(tool, MoviePilotApiTool):
            operation = resolve_api_operation(str(arguments.get("operation_id") or ""))
            if operation is None or operation.effect is not ActionEffect.SAFE_READ:
                return False
            try:
                normalized = tool.canonical_arguments(arguments)
            except (TypeError, ValueError):
                return False
            return not requests_system_setting_secrets(normalized)
        if isinstance(tool, McpExternalTool):
            return False
        name = getattr(tool, "name", None)
        if name in SUBAGENT_READ_ACTIONS:
            action = arguments.get("action")
            return bool(
                isinstance(action, str) and action in SUBAGENT_READ_ACTIONS[name]
                and not arguments.get("cookies") and not arguments.get("user_agent")
            )
        tags = set(getattr(tool, "tags", None) or [])
        return ToolTag.Read in tags and not tags.intersection({
            ToolTag.Write, ToolTag.Message, ToolTag.UserInteraction,
        })


__all__ = [
    "AgentPolicyMiddleware",
    "POLICY_DENIED_MESSAGE",
    "POLICY_UNAVAILABLE_MESSAGE",
]
