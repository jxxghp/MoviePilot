"""在副作用之前持久认领工具调用，并对未知写入执行只读核验。"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langchain.agents.middleware.types import AgentMiddleware, PrivateStateAttr, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, create_schema_from_function
from pydantic import Field

from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import ActionEffect, ExecutionOutcome, ToolPolicyContext
from app.agent.policy.sanitizer import sanitize_for_host, summarize_error
from app.agent.tools.base import run_agent_blocking
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool
from app.agent.tools.result import inspect_tool_result
from app.application.invocation import (
    InvocationConflictError,
    InvocationFinalStatus,
    InvocationIdentity,
    InvocationRepository,
    InvocationSnapshot,
)
from app.runtime.log import logger

GET_TOOL_EXECUTION_NAME = "get_tool_execution"


class InvocationState(TypedDict):
    """在同一轮模型工具循环内提供稳定去重范围，用户新请求使用新范围。"""

    invocation_turn_id: Annotated[str, PrivateStateAttr]


def _canonical_arguments(request: ToolCallRequest) -> dict[str, Any]:
    """利用实际工具 schema 规范默认值，保证同一写入的参数指纹稳定。"""
    arguments = dict(request.tool_call.get("args") or {})
    if isinstance(request.tool, MoviePilotApiTool):
        return dict(request.tool.canonical_arguments(arguments))
    schema = getattr(request.tool, "args_schema", None)
    if isinstance(schema, type) and hasattr(schema, "model_validate"):
        return dict(schema.model_validate(arguments).model_dump(mode="json"))
    return arguments


def _fingerprint(value: Any) -> str:
    """只存储规范 JSON 的单向摘要，不把写入参数或凭据写入回执。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# follow_imports=skip 下第三方中间件基类按 Any 处理，仅忽略 SDK 边界。
class InvocationMiddleware(AgentMiddleware):  # type: ignore[misc]
    """维护真实工具写入的持久回执；未认领、未知或已完成调用不会盲目重放。"""

    state_schema = InvocationState

    def __init__(self, context: ToolPolicyContext, repository: InvocationRepository, tools: list[Any]) -> None:
        """绑定组合根注入的持久端口及宿主用户身份。"""
        self.context = context
        self.repository = repository
        self._guarded_tools = tuple(tools)
        self.tools = [StructuredTool.from_function(
            name=GET_TOOL_EXECUTION_NAME,
            description=(
                "Read a durable tool execution receipt by invocation_id returned by a previous tool. "
                "A receipt contains host status only, never raw credentials or original output. "
                "Unknown outcomes require read-only reconciliation; querying does not retry an action."
            ),
            coroutine=self._get_execution,
            args_schema=create_schema_from_function("ToolExecutionInput", self._get_execution),
            tags=["agent_tool", "read"],
        )]
        object.__setattr__(self.tools[0], "_agent_tool_source", "middleware:invocation")

    async def abefore_agent(self, state: InvocationState, runtime: Any) -> dict[str, Any]:
        """新用户请求创建独立意图范围，同一轮中的重复 API 参数合并执行。"""
        return {"invocation_turn_id": uuid.uuid4().hex}

    def _identity(self, invocation_id: str) -> InvocationIdentity:
        """身份只来自宿主上下文，工具参数不能切换用户或会话。"""
        return InvocationIdentity(str(self.context.user_id or ""), self.context.session_id, invocation_id)

    def _is_write(self, request: ToolCallRequest) -> bool:
        """API 按操作副作用分类，其余工具沿用明确只读标签与内部能力边界。"""
        if request.tool is None or not any(request.tool is tool for tool in self._guarded_tools):
            return False
        if isinstance(request.tool, MoviePilotApiTool):
            operation = resolve_api_operation(str(request.tool_call.get("args", {}).get("operation_id", "")))
            return operation is not None and operation.effect not in {ActionEffect.SAFE_READ, ActionEffect.SENSITIVE_READ}
        if isinstance(request.tool, McpExternalTool) or str(getattr(request.tool, "_agent_tool_source", "")).startswith("mcp:"):
            return True
        if str(getattr(request.tool, "_agent_tool_source", "")).startswith("middleware:"):
            return False
        tags = set(getattr(request.tool, "tags", None) or [])
        return "write" in tags or "read" not in tags

    @staticmethod
    def _message(request: ToolCallRequest, outcome: str, message: str, **extra: Any) -> ToolMessage:
        """给模型明确结果状态和恢复动作，同时保留 LangChain 二态兼容字段。"""
        recovery = extra.pop("recovery", None)
        if recovery is None and outcome == "failed":
            recovery = "根据错误信息修正输入或改用正确工具后重试；不要重复提交未确认的写入。"
        elif recovery is None and outcome == "unknown":
            recovery = "结果未知时先调用只读查询或 get_tool_execution 核验实际状态，不要直接重试写入。"
        payload = {"execution_outcome": outcome, "message": message, **extra}
        if recovery is not None:
            payload["recovery"] = recovery
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=str(request.tool_call.get("name") or "unknown"),
            status="success" if outcome in {"succeeded", "pending"} else "error",
            additional_kwargs={"moviepilot_execution_outcome": outcome},
        )

    async def _get_execution(
        self, invocation_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> str:
        """只查询当前用户和会话的固定回执，不开放任意状态覆盖入口。"""
        record = await run_agent_blocking("db", self.repository.get, self._identity(invocation_id))
        if record is None:
            return json.dumps({"success": False, "error": "execution_not_found"})
        return json.dumps({
            "success": True, "invocation_id": record.identity.invocation_id,
            "tool_name": record.tool_name, "execution_status": record.status,
            "summary": record.summary, "updated_at": record.updated_at,
        }, ensure_ascii=False)

    async def _finish(self, record: InvocationSnapshot, status: InvocationFinalStatus) -> bool:
        """短事务收口失败仍保留原认领，后续调用不能因记录失败重放副作用。"""
        try:
            return bool(await run_agent_blocking(
                "db", self.repository.finish, record.identity,
                claim_token=record.claim_token, status=status,
            ))
        except Exception as error:
            logger.warning(f"保存工具执行回执失败: {type(error).__name__}")
            return False

    @staticmethod
    async def _verify_setting(request: ToolCallRequest) -> bool:
        """只对非敏感设置的完整替换核验当前值；不以猜测收口其他写操作。"""
        arguments = request.tool_call.get("args") or {}
        body = arguments.get("body")
        if (
            not isinstance(request.tool, MoviePilotApiTool)
            or arguments.get("operation_id") != "config.system.update"
            or not isinstance(body, dict) or body.get("operation", "replace") != "replace"
            or not body.get("setting_key") or "value" not in body
            or sanitize_for_host(body) != body
        ):
            return False
        try:
            result = await request.tool.ainvoke({
                "operation_id": "config.system.get",
                "query": {"setting_key": body["setting_key"], "include_values": True, "show_secrets": False},
            })
            payload = json.loads(result)
            items = payload.get("data", {}).get("settings", [])
            if payload.get("success") is not True or len(items) != 1:
                return False
            item = items[0]
            return bool(
                not item.get("redacted") and "value" in item
                and item.get("setting_key") == body["setting_key"]
                and _fingerprint(item["value"]) == _fingerprint(body["value"])
            )
        except Exception as error:
            logger.info(f"工具写入只读核验未完成: {type(error).__name__}")
            return False

    async def _existing(self, request: ToolCallRequest, record: InvocationSnapshot) -> ToolMessage:
        """已完成直接返回回执，未知结果仅在宿主只读核验成功后收口。"""
        if record.status == "unknown" and await self._verify_setting(request):
            if await self._finish(record, "succeeded"):
                return self._message(request, "succeeded", "已只读核验目标设置生效，没有重复写入。",
                                     invocation_id=record.identity.invocation_id, reconciled=True)
        outcome = "pending" if record.status == "running" else record.status
        return self._message(
            request, outcome,
            f"已有执行记录：{record.summary}。本次未重复执行；结果未知时请先核验实际状态。",
            invocation_id=record.identity.invocation_id, replayed=True,
        )

    @staticmethod
    def _attach_receipt(result: ToolMessage, record: InvocationSnapshot, outcome: ExecutionOutcome) -> ToolMessage:
        """保留业务字段并公开宿主回执编号，让模型能够调用状态查询工具。"""
        try:
            payload = json.loads(result.content) if isinstance(result.content, str) else None
        except (TypeError, ValueError):
            payload = None
        payload = dict(payload) if isinstance(payload, dict) else {"result": result.content}
        payload["_tool_execution"] = {"invocation_id": record.identity.invocation_id, "outcome": outcome.value}
        return result.model_copy(update={
            "content": json.dumps(payload, ensure_ascii=False),
            "additional_kwargs": {
                **result.additional_kwargs, "moviepilot_invocation_id": record.identity.invocation_id,
                "moviepilot_execution_outcome": outcome.value,
            },
        })

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在外部副作用前提交认领，跨重试保留真实已知与未知状态。"""
        if not self._is_write(request):
            return await handler(request)
        try:
            canonical_arguments = _canonical_arguments(request)
            arguments_digest = _fingerprint(canonical_arguments)
            if isinstance(request.tool, MoviePilotApiTool):
                request = request.override(tool_call={**request.tool_call, "args": canonical_arguments})
                previous = await run_agent_blocking(
                    "db", self.repository.find_unresolved, str(self.context.user_id or ""), self.context.session_id,
                    tool_name=request.tool.name, arguments_digest=arguments_digest,
                )
                if previous is not None:
                    return await self._existing(request, previous)
                invocation_id = _fingerprint([request.state.get("invocation_turn_id"), request.tool.name, arguments_digest])
            else:
                invocation_id = str(request.tool_call.get("id") or uuid.uuid4().hex)
            claim = await run_agent_blocking(
                "db", self.repository.claim, self._identity(invocation_id),
                tool_name=request.tool.name, arguments_digest=arguments_digest,
            )
        except InvocationConflictError:
            return self._message(request, "failed", "同一调用 ID 的工具或参数发生变化，未执行。")
        except (TypeError, ValueError) as error:
            detail = summarize_error(error, max_chars=240)
            logger.info(f"工具参数未通过 API 输入合同校验: {detail}")
            operation_id = str(request.tool_call.get("args", {}).get("operation_id", ""))
            input_contract = request.tool.get_operation_input_contract(operation_id)
            return self._message(
                request,
                "failed",
                f"{operation_id} 的输入未通过当前 operation 输入合同校验：{detail}。"
                "请按 input_contract 只提交允许字段并补齐 required 字段后再调用。",
                operation_id=operation_id,
                input_contract=input_contract,
            )
        except Exception as error:
            logger.warning(f"工具执行认领失败: {type(error).__name__}")
            return self._message(request, "failed", "无法持久记录本次写入，未执行工具。")
        if not claim.acquired:
            return await self._existing(request, claim.record)
        try:
            result = await handler(request)
        except (asyncio.CancelledError, TimeoutError):
            await self._finish(claim.record, "unknown")
            raise
        except Exception:
            await self._finish(claim.record, "unknown")
            return self._message(request, "unknown", "工具执行异常，写入结果未知，请先核验实际状态。",
                                 invocation_id=claim.record.identity.invocation_id)
        outcome = inspect_tool_result(result)
        status: InvocationFinalStatus = outcome.value
        settled = await self._finish(claim.record, status)
        if not settled:
            return self._message(request, "unknown", "工具已经返回，但持久回执未确认；请先核验，勿直接重试。",
                                 invocation_id=claim.record.identity.invocation_id)
        if isinstance(result, ToolMessage):
            return self._attach_receipt(result, claim.record, outcome)
        return result
