"""LangChain 工具调用的 MoviePilot 宿主策略中间件。"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest

from app.agent.policy import (
    DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    AgentToolPolicyOrchestrator,
    ToolPolicyContext,
    call_policy_hook,
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
    ) -> None:
        """绑定宿主可信上下文和共享策略编排器。"""
        self.context = context
        self.orchestrator = orchestrator

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
        observation = call_policy_hook(
            "start",
            self.orchestrator.start,
            context=self.context,
            tool=request.tool,
            arguments=arguments,
            invocation_id=tool_call.get("id"),
        )
        try:
            result = await handler(request)
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
        return result


__all__ = ["AgentPolicyMiddleware"]
