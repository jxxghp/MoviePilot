"""解析工具明确的结果协议，保持正常业务载荷和自然语言原样。"""

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.types import Command

from app.agent.policy.contracts import ExecutionOutcome

EXECUTION_OUTCOME_KEY = "moviepilot_execution_outcome"
_ERROR_ENVELOPE_KEYS = frozenset({
    "error", "message", "detail", "code", "status", "state", "success", "tool_name", "action",
    "execution_outcome", EXECUTION_OUTCOME_KEY,
})


# follow_imports=skip 下第三方异常基类按 Any 处理，仅忽略 SDK 边界。
class ToolExecutionError(ToolException):  # type: ignore[misc]
    """携带宿主已脱敏说明的可恢复工具故障，避免伪装为成功字符串。"""


def _aggregate_outcomes(outcomes: list[ExecutionOutcome]) -> ExecutionOutcome:
    """未知副作用优先于失败，未完成任务优先于纯成功回执。"""
    for outcome in (ExecutionOutcome.UNKNOWN, ExecutionOutcome.FAILED, ExecutionOutcome.PENDING):
        if outcome in outcomes:
            return outcome
    return ExecutionOutcome.SUCCEEDED


def _task_outcome(payload: Mapping[str, Any]) -> ExecutionOutcome:
    """只解析执行任务和终端会话的状态，不把下载器资源状态当成工具终态。"""
    state = payload.get("status", payload.get("state"))
    state = state if isinstance(state, str) else ""
    if state in {"pending", "queued", "running", "accepted", "in_progress", "starting"}:
        return ExecutionOutcome.PENDING
    if state in {"failed", "error", "cancelled", "canceled", "killed"}:
        return ExecutionOutcome.FAILED
    if state in {"unknown", "interrupted"}:
        return ExecutionOutcome.UNKNOWN
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return ExecutionOutcome.FAILED
    return ExecutionOutcome.SUCCEEDED


def _mapping_outcome(payload: Mapping[str, Any]) -> ExecutionOutcome:
    """识别明确错误 envelope 和既有任务协议，业务数据中的可选 error 不算失败。"""
    explicit = payload.get(EXECUTION_OUTCOME_KEY, payload.get("execution_outcome"))
    if isinstance(explicit, str) and explicit in ExecutionOutcome._value2member_map_:
        return ExecutionOutcome(explicit)
    if payload.get("isError") is True or payload.get("success") is False or payload.get("state") is False:
        return ExecutionOutcome.FAILED
    if (
        payload.get("error") and set(payload).issubset(_ERROR_ENVELOPE_KEYS)
        and payload.get("success") is not True and payload.get("state") is not True
    ):
        return ExecutionOutcome.FAILED
    if any(payload.get(key) for key in ("task_id", "operation_id", "execution_id")) or (
        payload.get("session_id") and "command" in payload and "exit_code" in payload
    ):
        return _task_outcome(payload)
    tasks = payload.get("tasks")
    action = payload.get("action")
    if isinstance(action, str) and action in {"start", "run", "pipeline", "status", "wait", "cancel"} and isinstance(tasks, list):
        return _aggregate_outcomes([
            _task_outcome(task) for task in tasks if isinstance(task, dict) and "task_id" in task
        ])
    return ExecutionOutcome.SUCCEEDED


def inspect_tool_result(result: Any) -> ExecutionOutcome:
    """从工具消息、Command 或 JSON 提取明确执行状态，不依据自然语言猜测。"""
    if isinstance(result, ToolMessage):
        explicit = result.additional_kwargs.get(EXECUTION_OUTCOME_KEY)
        if isinstance(explicit, str) and explicit in ExecutionOutcome._value2member_map_:
            return ExecutionOutcome(explicit)
        if result.status == "error":
            return ExecutionOutcome.FAILED
        return inspect_tool_result(result.content)
    if isinstance(result, Command):
        update = result.update
        if isinstance(update, dict):
            return _aggregate_outcomes([
                inspect_tool_result(message) for message in update.get("messages", [])
                if isinstance(message, ToolMessage)
            ])
        return ExecutionOutcome.SUCCEEDED
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            return ExecutionOutcome.SUCCEEDED
    if isinstance(result, Mapping):
        return _mapping_outcome(result)
    # MCP 单文本块可能携带结构化工具结果；多块正文与业务列表不递归猜测。
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        block = result[0]
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            return inspect_tool_result(block["text"])
    return ExecutionOutcome.SUCCEEDED


def annotate_tool_result(result: Any) -> Any:
    """将已识别状态附到工具协议元数据，成功载荷及 Command 状态更新保持不变。"""
    if isinstance(result, ToolMessage):
        outcome = inspect_tool_result(result)
        result.additional_kwargs[EXECUTION_OUTCOME_KEY] = outcome.value
        if outcome in {ExecutionOutcome.FAILED, ExecutionOutcome.UNKNOWN}:
            result.status = "error"
    elif isinstance(result, Command) and isinstance(result.update, dict):
        for message in result.update.get("messages", []):
            if isinstance(message, ToolMessage):
                annotate_tool_result(message)
    return result
