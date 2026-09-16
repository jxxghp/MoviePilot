"""为当前会话保留有界工具结果，并按字符位置继续读取。"""

import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, create_schema_from_function
from pydantic import Field

from app.agent.policy.contracts import ToolPolicyContext
from app.agent.tools.base import TOOL_RESULT_RECORDER, format_tool_result_for_agent
from app.agent.tools.result import inspect_tool_result

READ_TOOL_RESULT_NAME = "read_tool_result"
MAX_RESULT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * MAX_RESULT_BYTES
MAX_RESULTS = 8
RESULT_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class _StoredResult:
    """内存中短期保留原始授权结果，不复制到日志、记忆或磁盘。"""

    thread_id: str
    text: str
    byte_size: int
    tool_name: str
    expires_at: float
    requires_admin: bool


# follow_imports=skip 不分析第三方基类，仅在框架继承边界忽略 misc。
class ToolOutputMiddleware(AgentMiddleware):  # type: ignore[misc]
    """包装当前图的工具结果；读取按会话与当前管理员身份双重隔离。"""

    def __init__(self, context: ToolPolicyContext) -> None:
        """注册内部续读工具，缓存容量和寿命均受固定上限约束。"""
        self.context = context
        self._results: OrderedDict[str, _StoredResult] = OrderedDict()
        self._lock = threading.RLock()
        self.tools = [StructuredTool.from_function(
            name=READ_TOOL_RESULT_NAME,
            description=(
                "Read the next page of an archived tool result using its result_id and next_offset. "
                "Offsets count Unicode characters. Results expire after 15 minutes, on eviction, "
                "or when this conversation graph is rebuilt. Never guess result IDs."
            ),
            coroutine=self._read_result,
            args_schema=create_schema_from_function(
                "ReadToolResultInput", self._read_result, filter_args=["runtime"],
            ),
            tags=["agent_tool", "read"],
        )]
        object.__setattr__(self.tools[0], "_agent_tool_source", "middleware:output")

    @staticmethod
    def _thread_id(runtime: ToolRuntime) -> str:
        """使用图的真实线程身份，避免同一中间件被不同图线程误复用。"""
        return str(runtime.config.get("configurable", {}).get("thread_id") or "")

    def _prune(self) -> None:
        """调用方持锁时删除到期结果，并按写入顺序执行 UTF-8 字节容量淘汰。"""
        now = time.monotonic()
        for result_id, result in list(self._results.items()):
            if result.expires_at <= now:
                del self._results[result_id]
        size = sum(result.byte_size for result in self._results.values())
        while self._results and (len(self._results) > MAX_RESULTS or size > MAX_TOTAL_BYTES):
            _, result = self._results.popitem(last=False)
            size -= result.byte_size

    def _store(self, thread_id: str, tool_name: str, text: str, requires_admin: bool) -> dict[str, Any]:
        """只保存完整的有界结果，超过容量时明确说明无法续读。"""
        if not thread_id:
            return {"result_unavailable": "thread_unavailable"}
        # 先用字符数下界拒绝超大文本，避免为明显无法归档的输出再分配编码副本。
        if len(text) > MAX_RESULT_BYTES or (byte_size := len(text.encode("utf-8"))) > MAX_RESULT_BYTES:
            return {"result_unavailable": "result_too_large", "result_limit_bytes": MAX_RESULT_BYTES}
        result_id = uuid.uuid4().hex
        with self._lock:
            self._results[result_id] = _StoredResult(
                thread_id=thread_id, text=text, byte_size=byte_size, tool_name=tool_name,
                expires_at=time.monotonic() + RESULT_TTL_SECONDS,
                requires_admin=requires_admin,
            )
            self._prune()
        return {
            "result_id": result_id,
            "read_tool": READ_TOOL_RESULT_NAME,
            "expires_in_seconds": RESULT_TTL_SECONDS,
            "offset_unit": "unicode_characters",
        }

    async def _read_result(
        self,
        result_id: Annotated[str, Field(min_length=32, max_length=32)],
        runtime: ToolRuntime,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=16000)] = 4000,
    ) -> str:
        """按显式游标续读；过期、越权和未知 ID 使用同一不可用响应。"""
        with self._lock:
            self._prune()
            result = self._results.get(result_id)
        if (
            not self._thread_id(runtime) or result is None or result.thread_id != self._thread_id(runtime)
            or (result.requires_admin and not self.context.agent_context.get("is_admin"))
        ):
            return json.dumps({"success": False, "error": "result_unavailable"})
        if offset > len(result.text):
            return json.dumps({"success": False, "error": "offset_out_of_range", "total_chars": len(result.text)})
        end = min(offset + limit, len(result.text))
        return json.dumps({
            "success": True, "result_id": result_id, "tool_name": result.tool_name,
            "offset": offset, "next_offset": end if end < len(result.text) else None,
            "total_chars": len(result.text), "content": result.text[offset:end],
        }, ensure_ascii=False)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """让内置工具在截断前归档，并覆盖返回大文本的外部 MCP 工具。"""
        if request.tool_call.get("name") == READ_TOOL_RESULT_NAME:
            return await handler(request)
        thread_id = self._thread_id(request.runtime)
        requires_admin = bool(self.context.agent_context.get("is_admin"))
        token = TOOL_RESULT_RECORDER.set(lambda name, text: self._store(thread_id, name, text, requires_admin))
        try:
            result = await handler(request)
            if isinstance(result, ToolMessage) and isinstance(result.content, str):
                content = format_tool_result_for_agent(result.content, tool_name=result.name)
                if content != result.content:
                    payload = json.loads(content)
                    payload["execution_outcome"] = inspect_tool_result(result).value
                    content = json.dumps(payload, ensure_ascii=False, indent=2)
                return result.model_copy(update={"content": content})
            return result
        finally:
            TOOL_RESULT_RECORDER.reset(token)
