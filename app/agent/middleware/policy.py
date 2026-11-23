"""
LangChain 工具调用的 MoviePilot 宿主策略中间件。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, AgentState, ToolCallRequest, hook_config
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import (
    ActionEffect,
    AuthSource,
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
from app.agent.tools.base import run_agent_blocking
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool
from app.agent.tools.result import EXECUTION_OUTCOME_KEY, ToolExecutionError, annotate_tool_result
from app.agent.tools.tags import ToolTag
from app.schemas.types import NotificationChannel

if TYPE_CHECKING:
    from app.agent.middleware.guardrails import ToolGuardrailsMiddleware

POLICY_DENIED_MESSAGE = "当前宿主策略不允许执行该工具。"
POLICY_UNAVAILABLE_MESSAGE = "宿主策略暂时不可用，未执行该工具。"
TOOL_TIMEOUT_MESSAGE = "工具执行超时，已停止等待结果；若工具包含外部写操作，操作可能仍在继续，请先确认实际状态再重试。"
SUBAGENT_READ_ACTIONS = {
    "execute_command": frozenset({"read", "wait"}),
    "browse_webpage": frozenset({"snapshot", "get_content", "screenshot", "wait", "list_tabs"}),
    "persona": frozenset({"list"}),
    "agent_task": frozenset({"list"}),
}


class AgentPolicyMiddleware(AgentMiddleware):
    """
    观测进入本地 ToolNode 的 client-side 工具调用和结果。

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
        guardrails: "ToolGuardrailsMiddleware | None" = None,
    ) -> None:
        """
        绑定宿主可信上下文和共享策略编排器。
        """
        self.guardrails = guardrails
        self.context = context
        self.orchestrator = orchestrator
        self.catalog = catalog
        self._tools = {tool.name: tool for tool in (tools or []) if getattr(tool, "name", None)}

    @hook_config(can_jump_to=["end", "model"])
    async def aafter_model(self, state: AgentState, runtime: Any) -> Any:
        """
        复用原有模型后置节点合并检测更新，不消耗额外图递归步数。
        """
        paused = await self._sensitive_pause(state)
        if paused is not None:
            return paused
        return await self.guardrails.after_model_update(state, runtime) if self.guardrails else None

    async def _sensitive_pause(self, state: AgentState) -> Any:
        """
        敏感设置暂停优先于自动续行，绝不因纠偏放行未确认的调用。
        """
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

        paused_messages: list[AnyMessage] = [
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
        """
        在 handler 外层生成 shadow 决策和 secret-safe 回执摘要。
        """
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
        """
        把普通故障转为可恢复回执，异常私有正文不进入模型上下文。
        """
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
        """
        为子代理的只读拒绝返回可纠正的 operation 合同，而不是只给笼统权限错误。
        """
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
        """
        执行一次本地工具调用，并复用 ToolNode 的策略生命周期。
        """
        authorization_error = await self._channel_authorization_error(tool, arguments)
        if not authorization_error:
            authorization_error = self._web_authorization_error(tool)
        if authorization_error:
            return False, ToolMessage(content=authorization_error, tool_call_id=str(invocation_id or ""),
                                      name=str(getattr(tool, "name", "unknown")), status="error",
                                      additional_kwargs={EXECUTION_OUTCOME_KEY: ExecutionOutcome.FAILED.value})
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

    async def _channel_authorization_error(self, tool: Any, arguments: dict[str, Any]) -> str | None:
        """
        所有本地工具和子代理调用前复核渠道状态，覆盖记忆和外部 MCP 等非基础工具。
        """
        identity = self.context.agent_context.get("authorization_id")
        if not isinstance(identity, str) or not identity:
            if self.context.agent_context.get("channel_authorization_required") is True:
                from app.agent.policy.api import API_UNBOUND_READ_OPERATIONS

                public_operation = isinstance(tool, MoviePilotApiTool) and arguments.get("operation_id") in API_UNBOUND_READ_OPERATIONS
                if not public_operation and getattr(tool, "name", "") not in {"send_message", "send_voice_message"}:
                    return "此操作需要管理员授权，请发送“申请使用”，批准后重新发送要执行的操作。"
            return None
        from app.application.security.authorization import get_channel_authorization

        service = get_channel_authorization()
        if service is None:
            return "渠道授权服务不可用，未执行该工具。"
        try:
            capability = "schedule" if isinstance(self.context.agent_context.get("execution_user_id"), int) else None
            user = await run_agent_blocking("db", service.principal, identity, capability=capability)
            self.context.agent_context["is_admin"] = user.is_superuser
            if not user.is_superuser and (isinstance(tool, McpExternalTool) or str(getattr(tool, "_agent_tool_source", "")).startswith("plugin:")):
                return "此扩展工具需要渠道管理员授权。"
            return None
        except PermissionError as error:
            state = self.context.agent_context.setdefault("execution_identity_error", {})
            if isinstance(state, dict):
                state["message"] = str(error)
            return str(error)

    def _web_authorization_error(self, tool: Any) -> str | None:
        """
        对直接登录的网页会话复核账号激活状态及插件/管理员工具边界。

        :param tool: 即将执行的本地工具实例
        :return: 拒绝原因；放行时返回 None
        """
        is_web_session = self.context.auth_source is AuthSource.WEB_SESSION or (
            self.context.channel in {NotificationChannel.Web.value, NotificationChannel.WebAgent.value}
            and self.context.source not in {"api", "openai", "openai.responses", "anthropic"}
        )
        if not is_web_session:
            return None
        if self.context.agent_context.get("web_user_active") is False:
            return "当前 MoviePilot 用户已停用，请联系管理员启用该用户后重试。"
        if bool(self.context.agent_context.get("is_admin")):
            return None
        tool_source = str(getattr(tool, "_agent_tool_source", "") or "")
        tool_name = str(getattr(tool, "name", "") or "")
        if tool_source.startswith("plugin:") or tool_name.startswith("plugin:"):
            return "此插件工具仅允许系统管理员调用。"
        if getattr(tool, "_require_admin", getattr(tool, "require_admin", False)):
            return "抱歉，您没有权限执行此操作（仅限系统管理员）。"
        return None

    @staticmethod
    def _subagent_read_allowed(tool: Any, arguments: dict[str, Any]) -> bool:
        """
        按真实 API 操作限制子代理，MCP 的兼容 Read 标签不构成只读证明。
        """
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
        if name in {"knowledge_search", "knowledge_read"}:
            return False
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
