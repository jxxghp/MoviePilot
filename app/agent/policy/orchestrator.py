"""Agent 工具策略观测、脱敏回执与共享执行边界。"""

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any, Mapping, Optional, TypeVar

from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from app.agent.policy.contracts import (
    ActionEffect,
    ConfirmationMode,
    ExecutionOutcome,
    ExecutionReceipt,
    MigrationState,
    PolicyDecision,
    PolicyObservation,
    RecoveryMode,
    ResultSensitivity,
    ToolInvocation,
    ToolPolicyContext,
)
from app.agent.policy.registry import DEFAULT_TOOL_POLICY_REGISTRY, ToolPolicyRegistry
from app.agent.policy.sanitizer import (
    stable_type_name,
    summarize_error,
    summarize_input,
    summarize_result,
)
from app.runtime.log import logger

_HookResult = TypeVar("_HookResult")


def call_policy_hook(
    phase: str,
    hook: Callable[..., _HookResult],
    *args: Any,
    **kwargs: Any,
) -> Optional[_HookResult]:
    """以 fail-open 方式调用兼容观测 hook，故障只记录稳定类型。"""
    try:
        return hook(*args, **kwargs)
    except Exception as error:
        try:
            logger.warning(f"Agent工具策略观测失败: phase={phase}, error_type={stable_type_name(error)}")
        except Exception:
            pass
        return None


def _normalize_policy_arguments(tool: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """为策略生成 Pydantic 规范化副本，不改变真实执行参数。"""
    raw_arguments = dict(arguments or {})
    args_schema = getattr(tool, "args_schema", None)
    if not args_schema:
        return raw_arguments
    try:
        validated = args_schema.model_validate(raw_arguments)
        return validated.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError, ValidationError):
        # 实际 handler 仍负责既有参数错误语义；策略观测按原始值保守处理。
        return raw_arguments


def _result_payload(result: Any) -> Any:
    """从 LangChain 工具消息中提取模型可见结果供脱敏摘要使用。"""
    if isinstance(result, ToolMessage):
        return result.content
    return result


class AgentToolPolicyOrchestrator:
    """让 Agent middleware 与 direct manager 复用同一策略生命周期。"""

    def __init__(self, registry: ToolPolicyRegistry = DEFAULT_TOOL_POLICY_REGISTRY) -> None:
        """绑定工具策略解析表。"""
        self.registry = registry

    def start(
        self,
        *,
        context: ToolPolicyContext,
        tool: Any,
        arguments: Mapping[str, Any],
        invocation_id: Optional[str] = None,
    ) -> PolicyObservation:
        """解析调用策略，并创建不影响现有 allow 行为的观测对象。"""
        tool_name = str(getattr(tool, "name", None) or "unknown_tool")
        normalized_arguments = _normalize_policy_arguments(tool, arguments)
        policy = self.registry.resolve(
            tool_name=tool_name,
            arguments=normalized_arguments,
            requires_admin=bool(getattr(tool, "_require_admin", False)),
        )
        if policy.migration_state is MigrationState.LEGACY_SHADOW:
            decision = PolicyDecision(
                allowed=True,
                confirmation_required=False,
                shadow=True,
                reason_code="legacy_shadow_allow",
            )
        elif policy.confirmation is ConfirmationMode.REQUIRED:
            # 通用编排器保持 shadow；支持的 Agent 入口会在 ToolNode 前独立完成确认。
            decision = PolicyDecision(
                allowed=True,
                confirmation_required=False,
                shadow=True,
                reason_code="confirmation_policy_shadow_allow",
            )
        else:
            decision = PolicyDecision(
                allowed=True,
                confirmation_required=False,
                shadow=False,
                reason_code="safe_read_allow",
            )
        invocation = ToolInvocation(
            invocation_id=invocation_id or uuid.uuid4().hex,
            tool_name=tool_name,
            arguments=normalized_arguments,
            principal=context.principal,
            session_id=context.session_id,
            origin=context.origin,
            channel=context.channel,
            source=context.source,
        )
        input_summary = summarize_input(normalized_arguments)
        observation = PolicyObservation(
            invocation=invocation,
            policy=policy,
            decision=decision,
            input_summary=input_summary,
            started_at=time.monotonic(),
        )
        logger.debug(
            f"Agent工具策略: tool={tool_name}, origin={context.origin.value}, "
            f"decision={decision.reason_code}, input={input_summary}"
        )
        return observation

    @staticmethod
    def finish(observation: PolicyObservation, result: Any) -> ExecutionReceipt:
        """生成成功回执 envelope，并只记录脱敏结果摘要。"""
        if observation.policy.result_sensitivity is ResultSensitivity.SECRET:
            result_summary = '{"protected_result": "***"}'
        else:
            result_summary = summarize_result(_result_payload(result))
        receipt = ExecutionReceipt(
            invocation_id=observation.invocation.invocation_id,
            tool_name=observation.invocation.tool_name,
            origin=observation.invocation.origin,
            decision=observation.decision,
            outcome=ExecutionOutcome.SUCCEEDED,
            input_summary=observation.input_summary,
            result_summary=result_summary,
            duration_ms=max(
                0,
                int((time.monotonic() - observation.started_at) * 1000),
            ),
        )
        logger.info(
            f"Agent工具执行完成: tool={receipt.tool_name}, "
            f"origin={receipt.origin.value}, shadow={receipt.decision.shadow}, "
            f"duration_ms={receipt.duration_ms}, result={result_summary}"
        )
        return receipt

    @staticmethod
    def _uncertain_external_state(
        observation: PolicyObservation,
        error: BaseException,
    ) -> tuple[bool, bool]:
        """标记取消或超时后无法确认的写操作终态。"""
        interrupted = isinstance(error, (asyncio.CancelledError, TimeoutError))
        read_only = observation.policy.effect in {
            ActionEffect.SAFE_READ,
            ActionEffect.SENSITIVE_READ,
        }
        if not interrupted or read_only:
            return False, False
        needs_reconcile = observation.policy.recovery not in {
            RecoveryMode.TRANSACTION,
            RecoveryMode.IDEMPOTENT,
        }
        return True, needs_reconcile

    @staticmethod
    def fail(observation: PolicyObservation, error: BaseException) -> ExecutionReceipt:
        """生成失败回执 envelope，不把异常中的凭据写入日志。"""
        error_summary = summarize_error(error)
        external_may_continue, needs_reconcile = AgentToolPolicyOrchestrator._uncertain_external_state(
            observation, error
        )
        receipt = ExecutionReceipt(
            invocation_id=observation.invocation.invocation_id,
            tool_name=observation.invocation.tool_name,
            origin=observation.invocation.origin,
            decision=observation.decision,
            outcome=ExecutionOutcome.FAILED,
            input_summary=observation.input_summary,
            error_summary=error_summary,
            duration_ms=max(
                0,
                int((time.monotonic() - observation.started_at) * 1000),
            ),
            external_may_continue=external_may_continue,
            needs_reconcile=needs_reconcile,
        )
        logger.error(
            f"Agent工具执行失败: tool={receipt.tool_name}, "
            f"origin={receipt.origin.value}, shadow={receipt.decision.shadow}, "
            f"duration_ms={receipt.duration_ms}, error={error_summary}, "
            f"external_may_continue={receipt.external_may_continue}, "
            f"needs_reconcile={receipt.needs_reconcile}"
        )
        return receipt


DEFAULT_TOOL_POLICY_ORCHESTRATOR = AgentToolPolicyOrchestrator()


__all__ = [
    "AgentToolPolicyOrchestrator",
    "DEFAULT_TOOL_POLICY_ORCHESTRATOR",
    "call_policy_hook",
]
