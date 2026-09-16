"""有界工具结果归档、Unicode 续读和权限作用域的真实图回归测试。"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field

from app.agent.middleware import output as output_module
from app.agent.middleware.output import (
    MAX_RESULT_BYTES,
    MAX_RESULTS,
    MAX_TOTAL_BYTES,
    READ_TOOL_RESULT_NAME,
    RESULT_TTL_SECONDS,
    ToolOutputMiddleware,
)
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.base import TOOL_RESULT_RECORDER, format_tool_result_for_agent, serialize_tool_result_for_agent
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.result import EXECUTION_OUTCOME_KEY


class _ToolModel(FakeMessagesListChatModel):
    """提供真实工具绑定而不访问外部模型。"""

    def bind_tools(self, _tools: list[Any], **_kwargs: Any) -> "_ToolModel":
        """保留确定性响应供真实 ToolNode 执行。"""
        return self


class _PagingModel(_ToolModel):
    """仅根据工具实际返回的归档 ID 和字符游标发起连续读取。"""

    pieces: list[str] = Field(default_factory=list)
    page_offsets: list[int] = Field(default_factory=list)
    result_id: str = ""

    def _generate(self, messages: list[Any], *_args: Any, **_kwargs: Any) -> ChatResult:
        """先执行源工具一次，再按回执读取每页直到恢复全部原文。"""
        last = messages[-1]
        if not isinstance(last, ToolMessage):
            response = _call("large_query", {}, "original")
        else:
            payload = json.loads(last.content)
            if last.name == "large_query":
                self.result_id = payload["result_id"]
                self.pieces.append(payload["content_preview"])
                offset = payload["returned_chars"]
            else:
                assert payload["success"] is True
                self.pieces.append(payload["content"])
                offset = payload["next_offset"]
            if offset is None:
                response = AIMessage(content="完整结果已读取。")
            else:
                self.page_offsets.append(offset)
                response = _call(READ_TOOL_RESULT_NAME, {
                    "result_id": self.result_id, "offset": offset, "limit": 4000,
                }, f"page-{offset}")
        return ChatResult(generations=[ChatGeneration(message=response)])


def _context(admin: bool = False) -> ToolPolicyContext:
    """构造可在缓存图执行间刷新管理员角色的宿主上下文。"""
    return ToolPolicyContext(
        session_id="output-session", user_id="user", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.WEB_SESSION,
        agent_context={"is_admin": admin},
    )


def _call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    """构造模型提交给真实 ToolNode 的标准工具调用。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id}])


def _graph_config(thread_id: Optional[str]) -> dict[str, Any]:
    """生成调用作用域；缺失线程身份必须被归档层拒绝。"""
    return {"configurable": {"thread_id": thread_id}} if thread_id else {}


def _read_in_graph(middleware: ToolOutputMiddleware, result_id: str, thread_id: Optional[str] = "owner", **kwargs: Any) -> dict[str, Any]:
    """通过真实工具参数验证与运行时注入读取一页归档。"""
    model = _ToolModel(responses=[
        _call(READ_TOOL_RESULT_NAME, {"result_id": result_id, **kwargs}, "read"), AIMessage(content="已检查。"),
    ])
    graph = create_agent(model=model, middleware=[middleware])
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="读取结果")]}, _graph_config(thread_id)))
    message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    return json.loads(message.content)


def _source_once(middleware: ToolOutputMiddleware, text: str, thread_id: Optional[str] = "owner") -> ToolMessage:
    """执行原样返回文本的外部工具，观察归档中间件最终提供给模型的回执。"""
    async def source() -> str:
        """模拟外部 MCP 工具的原始文本结果。"""
        return text

    tool = StructuredTool.from_function(coroutine=source, name="large_query")
    model = _ToolModel(responses=[_call(tool.name, {}, "original"), AIMessage(content="已检查。")])
    graph = create_agent(model=model, tools=[tool], middleware=[middleware])
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="查询数据")]}, _graph_config(thread_id)))
    return next(message for message in result["messages"] if isinstance(message, ToolMessage))


@pytest.mark.parametrize("builtin_formatter", [True, False])
def test_large_unicode_json_is_restored_across_pages_without_repeating_original_tool(builtin_formatter: bool):
    """内置截断前回调和外部大结果均能多页还原，原工具实际只执行一次。"""
    payload = {"success": True, "items": ["电影🎬Ω\n\"\\" * 12000], "count": 1}
    original = serialize_tool_result_for_agent(payload)
    calls = []

    async def source() -> str:
        """记录实际执行次数，并分别模拟内置工具与外部原始结果。"""
        calls.append("executed")
        return format_tool_result_for_agent(payload, tool_name="large_query") if builtin_formatter else original

    tool = StructuredTool.from_function(coroutine=source, name="large_query")
    middleware = ToolOutputMiddleware(_context())
    model = _PagingModel(responses=[AIMessage(content="unused")])
    graph = create_agent(model=model, tools=[tool], middleware=[middleware])
    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="完整读取查询结果")]}, {
        **_graph_config("owner"), "recursion_limit": 150,
    }))

    assert calls == ["executed"]
    assert len(model.page_offsets) > 2
    assert model.page_offsets == sorted(set(model.page_offsets))
    assert "".join(model.pieces) == original
    assert json.loads("".join(model.pieces)) == payload
    assert len(middleware._results) == 1
    assert _read_in_graph(middleware, model.result_id, offset=len(original))["next_offset"] is None
    assert _read_in_graph(middleware, model.result_id, offset=len(original) + 1)["error"] == "offset_out_of_range"


def test_archived_result_is_isolated_between_threads_and_admin_downgrades():
    """不同线程、未知结果及管理员降权均返回相同不可用回执。"""
    middleware = ToolOutputMiddleware(_context(admin=True))
    result = json.loads(_source_once(middleware, "机密内容" * 20000).content)
    result_id = result["result_id"]
    assert _read_in_graph(middleware, result_id)["success"] is True
    denied = {"success": False, "error": "result_unavailable"}
    assert _read_in_graph(middleware, result_id, thread_id="other") == denied
    assert _read_in_graph(middleware, "0" * 32) == denied
    middleware.context.agent_context["is_admin"] = False
    assert _read_in_graph(middleware, result_id) == denied


def test_missing_thread_identity_disables_archiving_and_reading():
    """无法证明会话归属时不能把多个无标识调用放入同一归档作用域。"""
    middleware = ToolOutputMiddleware(_context())
    result = json.loads(_source_once(middleware, "记录" * 40000, thread_id=None).content)
    assert result["result_unavailable"] == "thread_unavailable"
    assert "result_id" not in result
    assert middleware._results == {}
    assert _read_in_graph(middleware, "0" * 32, thread_id=None) == {"success": False, "error": "result_unavailable"}


def test_result_expires_at_ttl_without_extending_lifetime_on_read(monkeypatch):
    """读取不延长敏感结果寿命，恰好达到有效期即不可再取。"""
    clock = [100.0]
    monkeypatch.setattr(output_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    middleware = ToolOutputMiddleware(_context())
    result_id = middleware._store("owner", "query", "归档内容", False)["result_id"]
    clock[0] += RESULT_TTL_SECONDS - 1
    assert _read_in_graph(middleware, result_id)["success"] is True
    clock[0] += 1
    assert _read_in_graph(middleware, result_id)["error"] == "result_unavailable"
    assert middleware._results == {}


@pytest.mark.parametrize("capacity", ["count", "bytes"])
def test_result_capacity_evicts_oldest_without_exceeding_total_limits(capacity: str):
    """数量和总字节上限独立生效，淘汰最早结果且仍能读取最新归档。"""
    middleware = ToolOutputMiddleware(_context())
    if capacity == "count":
        text, count = "短内容", MAX_RESULTS + 1
    else:
        unit = "中文🎬"
        text, count = unit * (MAX_RESULT_BYTES // len(unit.encode("utf-8")) - 100), 5
    ids = [middleware._store("owner", "query", text, False)["result_id"] for _ in range(count)]
    assert _read_in_graph(middleware, ids[0])["error"] == "result_unavailable"
    assert _read_in_graph(middleware, ids[-1])["content"] == text[:4000]
    assert len(middleware._results) <= MAX_RESULTS
    assert sum(result.byte_size for result in middleware._results.values()) <= MAX_TOTAL_BYTES


@pytest.mark.parametrize("unicode_text", [True, False])
def test_single_oversized_result_reports_unavailable_instead_of_partial_archive(unicode_text: bool):
    """单条 UTF-8 结果超过一 MiB 时不保存半份内容，并返回明确的续读失败原因。"""
    middleware = ToolOutputMiddleware(_context())
    text = "电影" * (MAX_RESULT_BYTES // 6 + 1) if unicode_text else "x" * (MAX_RESULT_BYTES + 1)
    assert len(text.encode("utf-8")) > MAX_RESULT_BYTES
    if unicode_text:
        assert len(text) < MAX_RESULT_BYTES
    result = json.loads(_source_once(middleware, text).content)
    assert result["tool_result_truncated"] is True
    assert result["result_unavailable"] == "result_too_large"
    assert result["result_limit_bytes"] == MAX_RESULT_BYTES
    assert "result_id" not in result
    assert middleware._results == {}


@pytest.mark.parametrize("text", ["普通中文结果", '{"success": true, "value": 42}'])
def test_small_results_remain_byte_for_byte_unchanged(text: str):
    """普通短结果不归档、不加包装，也不改变原有业务返回格式。"""
    middleware = ToolOutputMiddleware(_context())
    assert _source_once(middleware, text).content == text
    assert middleware._results == {}


def test_truncated_failure_json_retains_failed_execution_outcome():
    """大失败 JSON 的预览仍声明执行失败，归档不能把失败改成成功。"""
    middleware = ToolOutputMiddleware(_context())
    text = json.dumps({"success": False, "error": "失败详情" * 20000}, ensure_ascii=False)
    result = json.loads(_source_once(middleware, text).content)
    assert result["execution_outcome"] == "failed"
    assert result["result_id"]
    assert _read_in_graph(middleware, result["result_id"])["content"] == text[:4000]


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_truncated_plain_text_preserves_source_error_metadata(outcome: str):
    """原始工具消息的故障或不确定状态不能被纯文本预览误写成成功。"""
    middleware = ToolOutputMiddleware(_context())
    request = SimpleNamespace(tool_call={"name": "source"}, runtime=SimpleNamespace(config=_graph_config("owner")))

    async def source(_request: Any) -> ToolMessage:
        """模拟通过消息协议声明执行状态的外部工具。"""
        return ToolMessage(
            content="执行详情" * 20000, tool_call_id="source", name="source", status="error",
            additional_kwargs={EXECUTION_OUTCOME_KEY: outcome} if outcome == "unknown" else {},
        )

    result = asyncio.run(middleware.awrap_tool_call(request, source))
    assert json.loads(result.content)["execution_outcome"] == outcome
    assert result.status == "error"


@pytest.mark.parametrize("failure", [RuntimeError("source failed"), asyncio.CancelledError()])
def test_source_failure_or_cancellation_restores_recorder_context(failure: BaseException):
    """同一执行上下文发生异常或取消后，归档回调必须恢复外层值。"""
    middleware = ToolOutputMiddleware(_context())
    request = SimpleNamespace(tool_call={"name": "source"}, runtime=SimpleNamespace(config=_graph_config("owner")))

    async def execute() -> None:
        """在同一任务内检查 finally，避免异步任务的上下文隔离掩盖泄漏。"""
        def previous(_name: str, _text: str) -> dict[str, Any]:
            """提供应在调用结束后恢复的外层归档回调。"""
            return {"outer": True}

        token = TOOL_RESULT_RECORDER.set(previous)
        try:
            async def fail(_request: Any) -> Any:
                """验证调用过程中已注入回调，再模拟源工具失败。"""
                assert TOOL_RESULT_RECORDER.get() is not previous
                raise failure

            with pytest.raises(type(failure)):
                await middleware.awrap_tool_call(request, fail)
            assert TOOL_RESULT_RECORDER.get() is previous
        finally:
            TOOL_RESULT_RECORDER.reset(token)

    asyncio.run(execute())


def test_admin_downgrade_during_execution_keeps_original_result_restriction():
    """敏感工具执行期间角色降低时，归档仍保留调用开始时的管理员限制。"""
    middleware = ToolOutputMiddleware(_context(admin=True))
    request = SimpleNamespace(tool_call={"name": "source"}, runtime=SimpleNamespace(config=_graph_config("owner")))

    async def source(_request: Any) -> ToolMessage:
        """模拟管理员操作完成前发生身份刷新。"""
        middleware.context.agent_context["is_admin"] = False
        return ToolMessage(content=format_tool_result_for_agent("管理员结果" * 20000, tool_name="source"), tool_call_id="source")

    result = asyncio.run(middleware.awrap_tool_call(request, source))
    result_id = json.loads(result.content)["result_id"]
    assert _read_in_graph(middleware, result_id)["error"] == "result_unavailable"


def test_result_reader_schema_and_catalog_exclude_injected_runtime():
    """续读工具身份可进入严格目录，宿主运行时不出现在模型可提供的参数中。"""
    middleware = ToolOutputMiddleware(_context())
    catalog = ToolCatalogSnapshot.from_tools(middleware.tools, plugin_revision=0, factory_revision="test").require_unique()
    assert catalog.entries[0].source == "middleware:output"
    assert set(middleware.tools[0].tool_call_schema.model_json_schema()["properties"]) == {"result_id", "offset", "limit"}
