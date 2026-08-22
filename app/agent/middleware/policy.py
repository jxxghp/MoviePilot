"""LangChain 工具调用的 MoviePilot 宿主策略中间件。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.policy.contracts import (
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.orchestrator import (
    DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    AgentToolPolicyOrchestrator,
    call_policy_hook,
)
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool


POLICY_DENIED_MESSAGE = "当前宿主策略不允许执行该工具。"
POLICY_UNAVAILABLE_MESSAGE = "宿主策略暂时不可用，未执行该工具。"
TOOL_TIMEOUT_MESSAGE = (
    "工具执行超时，已停止等待结果；"
    "若工具包含外部写操作，操作可能仍在继续，请先确认实际状态再重试。"
)


class AgentPolicyMiddleware(AgentMiddleware):
    """观测进入本地 ToolNode 的 client-side 工具调用和结果。

    模型供应商原生 server tools 在供应商侧执行，不经过本地 middleware，
    因而不具备这里生成的 start/finish/fail 回执。
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
        self._tools = {
            tool.name: tool
            for tool in (tools or [])
            if getattr(tool, "name", None)
        }

    @hook_config(can_jump_to=["end"])
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
                isinstance(tool, QuerySystemSettingsTool)
                and isinstance(arguments, dict)
                and arguments.get("show_secrets") is True
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
                content=(
                    "本轮工具调用已暂停，未执行任何操作；"
                    "请等待用户确认或取消敏感设置读取。"
                ),
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
        try:
            _, result = await self.execute_tool_call(
                tool=request.tool,
                arguments=arguments,
                invocation_id=tool_call.get("id"),
                handler=lambda: handler(request),
                enforce_decision=False,
            )
        except TimeoutError:
            tool_name = str(getattr(request.tool, "name", None) or "unknown")
            return ToolMessage(
                content=TOOL_TIMEOUT_MESSAGE,
                tool_call_id=str(tool_call.get("id") or ""),
                name=tool_name,
                status="error",
            )
        # 普通 ToolNode 保持 shadow 观测；已确认调用使用默认的强制决策语义。
        return result

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
        if (
                enforce_decision
                and observation.decision.allowed is False
        ):
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
            if observation is not None:
                call_policy_hook(
                    "fail",
                    self.orchestrator.fail,
                    observation,
                    error,
                )
            raise
        if observation is not None:
            call_policy_hook(
                "finish",
                self.orchestrator.finish,
                observation,
                result,
            )
        return True, result


__all__ = [
    "AgentPolicyMiddleware",
    "POLICY_DENIED_MESSAGE",
    "POLICY_UNAVAILABLE_MESSAGE",
]
