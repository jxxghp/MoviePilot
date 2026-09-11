"""真实 Agent 图与 SQLite 回执联合验证写工具防重和未知结果核验。"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.mcp import AgentMcpToolSpec
from app.agent.middleware.invocation import GET_TOOL_EXECUTION_NAME, InvocationMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.base import ToolExecutionTimeoutError
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool, create_external_mcp_tools
from app.application.invocation import InvocationIdentity
from app.db.adapters.invocation import TransactionalInvocationRepository
from app.db.models.agentinvocation import AgentInvocation
from app.schemas.agent import AgentMcpServerConfig

WRITE_ARGUMENTS = {"operation_id": "download.add", "body": {
    "torrent_in": {"title": "Test Movie", "enclosure": "https://example.invalid/test.torrent"},
}}


class _ScriptModel(FakeMessagesListChatModel):
    """按固定响应驱动真实图，记录模型绑定的实际工具实例。"""

    bound_tools: list[list[Any]] = Field(default_factory=list)

    def bind_tools(self, tools, **_kwargs):
        """接受真实 ToolNode 的工具声明，无需访问任何模型供应商。"""
        self.bound_tools.append(list(tools))
        return self


@pytest.fixture
def invocation_runtime(tmp_path):
    """提供线程可见的真实 SQLite 文件及只读回执快照函数。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'middleware.db'}", connect_args={"timeout": 20})
    AgentInvocation.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    repository = TransactionalInvocationRepository(factory)

    def records():
        """在独立连接读取持久化事实，避免只断言模拟端口调用。"""
        with factory() as session:
            return [row.to_dict() for row in session.execute(select(AgentInvocation).order_by(AgentInvocation.id)).scalars()]

    yield repository, records
    engine.dispose()


def _context(user_id="owner", session_id="invocation-chat"):
    """以宿主身份约束所有回执查询和写入。"""
    return ToolPolicyContext(
        session_id=session_id, user_id=user_id, origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL,
        agent_context={"is_admin": True},
    )


def _call(call_id, arguments=None, name="moviepilot_api"):
    """生成模型标准工具调用，参数默认是固定下载写操作。"""
    return {"id": call_id, "name": name, "args": WRITE_ARGUMENTS if arguments is None else arguments}


def _graph(repository, responses, *, context=None, tools=None):
    """使用生产顺序的策略和持久回执中间件构造可重复调用的真实图。"""
    context = context or _context()
    tools = tools if tools is not None else [MoviePilotApiTool(session_id=context.session_id, user_id=context.user_id)]
    middleware = InvocationMiddleware(context, repository, tools)
    model = _ScriptModel(responses=responses)
    graph = create_agent(
        model=model, tools=tools,
        middleware=[AgentPolicyMiddleware(context=context, tools=tools), middleware],
        checkpointer=InMemorySaver(),
    )
    return graph, model, middleware


async def _invoke(graph, prompt="请执行操作", thread_id="graph-thread"):
    """以真实用户消息进入图，触发每轮私有意图身份更新。"""
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        {"configurable": {"thread_id": thread_id}},
    )


def _tool_messages(result):
    """提取实际进入模型历史的工具响应。"""
    return [message for message in result["messages"] if isinstance(message, ToolMessage)]


@pytest.mark.asyncio
async def test_parallel_equivalent_api_calls_execute_once_but_new_request_can_repeat(invocation_runtime, monkeypatch):
    """并行同参数且默认字段写法不同只执行一次，下一条用户意图仍可有意重做。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value=json.dumps({"success": True, "data": {"id": "download-1"}}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, model, middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[
            _call("first"),
            _call("duplicate", {**WRITE_ARGUMENTS, "path_params": {}, "query": {}}),
        ]),
        AIMessage(content="已提交"),
        AIMessage(content="", tool_calls=[_call("intentional-repeat")]),
        AIMessage(content="按新的请求再次提交"),
    ])
    first = await _invoke(graph)
    assert run.await_count == 1
    assert len(records()) == 1
    assert records()[0]["status"] == "succeeded"
    messages = _tool_messages(first)
    assert len(messages) == 2
    invocation_ids = {
        payload.get("invocation_id", payload.get("_tool_execution", {}).get("invocation_id"))
        for payload in (json.loads(message.content) for message in messages)
    }
    assert invocation_ids == {records()[0]["invocation_id"]}
    await _invoke(graph, "我确认需要再添加一次")
    assert run.await_count == 2
    assert len(records()) == 2
    assert len({record["invocation_id"] for record in records()}) == 2
    assert middleware.tools[0] in model.bound_tools[0]


@pytest.mark.asyncio
async def test_unknown_api_result_blocks_same_input_in_new_turn(invocation_runtime, monkeypatch):
    """投递结果未知时保留恢复状态，跨用户请求不能直接重放原写入。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value=json.dumps({"execution_outcome": "unknown", "task_id": "job-1"}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("start")]), AIMessage(content="等待核验"),
        AIMessage(content="", tool_calls=[_call("retry")]), AIMessage(content="先核验状态"),
    ])
    first = await _invoke(graph)
    assert _tool_messages(first)[0].additional_kwargs["moviepilot_execution_outcome"] == "unknown"
    assert records()[0]["status"] == "unknown"
    result = await _invoke(graph, "重试刚才的下载")
    assert run.await_count == 1
    assert len(records()) == 1
    replay = json.loads(_tool_messages(result)[-1].content)
    assert replay["execution_outcome"] == "unknown"
    assert replay["replayed"] is True


@pytest.mark.asyncio
async def test_confirmed_submission_deduplicates_turn_but_allows_new_intent(invocation_runtime, monkeypatch):
    """明确接受的异步提交保留 pending；同一轮不重复，新用户意图可再次提交。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value=json.dumps({"execution_outcome": "pending", "task_id": "job-1"}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("submit")]),
        AIMessage(content="", tool_calls=[_call("same-turn-repeat")]),
        AIMessage(content="已提交，等待后台任务完成"),
        AIMessage(content="", tool_calls=[_call("new-user-intent")]),
        AIMessage(content="按新的明确请求再次提交"),
    ])
    first = await _invoke(graph)
    messages = _tool_messages(first)
    assert run.await_count == 1
    assert len(records()) == 1
    assert records()[0]["status"] == "pending"
    assert all(message.additional_kwargs["moviepilot_execution_outcome"] == "pending" for message in messages)
    repeated = json.loads(messages[-1].content)
    assert repeated["replayed"] is True
    assert repeated["invocation_id"] == records()[0]["invocation_id"]
    await _invoke(graph, "我明确需要再次提交同一个后台任务")
    assert run.await_count == 2
    assert len(records()) == 2
    assert {record["status"] for record in records()} == {"pending"}


@pytest.mark.asyncio
async def test_timeout_remains_unknown_and_new_turn_does_not_reexecute(invocation_runtime, monkeypatch):
    """真实工具超时经策略层转为未知消息，已认领的副作用不会再次执行。"""
    repository, records = invocation_runtime
    run = AsyncMock(side_effect=ToolExecutionTimeoutError("timed out"))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("timeout")]), AIMessage(content="查询状态"),
        AIMessage(content="", tool_calls=[_call("retry")]), AIMessage(content="不能盲目重复"),
    ])
    first = await _invoke(graph)
    assert _tool_messages(first)[0].additional_kwargs["moviepilot_execution_outcome"] == "unknown"
    assert records()[0]["status"] == "unknown"
    await _invoke(graph, "请继续刚才的任务")
    assert run.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("setting_key,current_value,redacted,operation,expected_reads,reconciled", [
    ("PROJECT_NAME", "My MoviePilot", False, "replace", 1, True),
    ("PROJECT_NAME", "Another name", False, "replace", 1, False),
    ("PROJECT_NAME", "My MoviePilot", True, "replace", 1, False),
    ("API_TOKEN", "My MoviePilot", False, "replace", 0, False),
    ("PROJECT_NAME", "My MoviePilot", False, "merge_dict", 0, False),
])
async def test_setting_unknown_is_reconciled_only_by_safe_matching_read(
    invocation_runtime, monkeypatch, setting_key, current_value, redacted, operation, expected_reads, reconciled,
):
    """只有非敏感完整替换且只读确认实际值一致时才收口，其他情况保持未知。"""
    repository, records = invocation_runtime
    operations = []

    async def run(_self, operation_id, **kwargs):
        """首次写入模拟结果丢失，核验必须调用同一个工具的真实只读入口。"""
        operations.append((operation_id, kwargs))
        if operation_id == "config.system.update":
            return json.dumps({"execution_outcome": "unknown"})
        assert operation_id == "config.system.get"
        return json.dumps({"success": True, "data": {"settings": [{
            "setting_key": setting_key, "value": current_value, "redacted": redacted,
        }]}})

    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    arguments = {"operation_id": "config.system.update", "body": {
        "setting_key": setting_key, "value": "My MoviePilot", "operation": operation,
    }}
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("setting-write", arguments)]), AIMessage(content="等待确认"),
        AIMessage(content="", tool_calls=[_call("setting-retry", arguments)]), AIMessage(content="已核验"),
    ])
    await _invoke(graph)
    result = await _invoke(graph, "确认刚才的设置是否生效")
    assert [operation for operation, _kwargs in operations].count("config.system.update") == 1
    reads = [kwargs for operation, kwargs in operations if operation == "config.system.get"]
    assert len(reads) == expected_reads
    if reads:
        assert reads[0]["query"] == {"setting_key": setting_key, "include_values": True, "show_secrets": False}
    assert len(records()) == 1
    assert records()[0]["status"] == ("succeeded" if reconciled else "unknown")
    payload = json.loads(_tool_messages(result)[-1].content)
    assert payload.get("reconciled", False) is reconciled


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_method", ["find_unresolved", "claim", "finish"])
async def test_persistence_failure_never_grants_unrecorded_or_repeat_execution(
    invocation_runtime, monkeypatch, failing_method,
):
    """认领失败不执行，收口失败保留认领并阻止下一轮重复副作用。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value=json.dumps({"success": True}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)

    def unavailable(*_args, **_kwargs):
        """模拟真实仓储事务故障，原仓储其他操作仍访问 SQLite。"""
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(repository, failing_method, unavailable)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("first")]), AIMessage(content="状态未确认"),
        AIMessage(content="", tool_calls=[_call("second")]), AIMessage(content="不能重复"),
    ])
    await _invoke(graph)
    await _invoke(graph, "继续操作")
    assert run.await_count == (1 if failing_method == "finish" else 0)
    assert len(records()) == (1 if failing_method == "finish" else 0)
    if failing_method == "finish":
        assert records()[0]["status"] == "running"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_preserves_unknown_receipt(invocation_runtime, monkeypatch):
    """取消图任务必须传播到调用方，同时记录已开始写入的未知结果。"""
    repository, records = invocation_runtime
    started = asyncio.Event()

    async def run(_self, **_kwargs):
        """模拟已发出副作用但结果尚未返回的工具。"""
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [AIMessage(content="", tool_calls=[_call("cancel")])])
    task = asyncio.create_task(_invoke(graph))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(records()) == 1
    assert records()[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_receipt_query_is_bound_to_host_owner_and_never_journaled(invocation_runtime, monkeypatch):
    """真实绑定的回执查询只读工具不能读取其他用户或会话，也不产生新回执。"""
    repository, records = invocation_runtime
    original = repository.claim(
        InvocationIdentity("owner", "invocation-chat", "known-call"),
        tool_name="moviepilot_api", arguments_digest="a" * 64,
    )
    repository.finish(original.record.identity, claim_token=original.record.claim_token, status="succeeded")
    run = AsyncMock(return_value=json.dumps({"success": True, "data": []}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    for context, expected in ((_context(), True), (_context("another-user"), False), (_context(session_id="other-chat"), False)):
        graph, _model, _middleware = _graph(repository, [
            AIMessage(content="", tool_calls=[
                _call("receipt", {"invocation_id": "known-call"}, GET_TOOL_EXECUTION_NAME),
                _call("read-api", {"operation_id": "scheduler.list"}),
            ]), AIMessage(content="查询结束"),
        ], context=context)
        result = await _invoke(graph)
        receipt = next(message for message in _tool_messages(result) if message.name == GET_TOOL_EXECUTION_NAME)
        assert json.loads(receipt.content)["success"] is expected
    assert run.await_count == 3
    assert len(records()) == 1


@pytest.mark.asyncio
async def test_unresolved_write_does_not_block_another_host_user_or_session(invocation_runtime, monkeypatch):
    """相同工具参数的未知写入只阻止原用户会话，模型参数无法替换宿主 owner。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value=json.dumps({"execution_outcome": "unknown"}))
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    contexts = [_context(), _context("another-user"), _context(session_id="other-chat")]
    for context in contexts:
        arguments = {**WRITE_ARGUMENTS, "user_id": "injected-user", "session_id": "injected-session"}
        graph, _model, _middleware = _graph(repository, [
            AIMessage(content="", tool_calls=[_call("same-provider-call-id", arguments)]),
            AIMessage(content="待核验"),
        ], context=context)
        await _invoke(graph)
    assert run.await_count == 3
    assert {(row["principal_id"], row["session_id"]) for row in records()} == {
        (context.user_id, context.session_id) for context in contexts
    }


@pytest.mark.asyncio
async def test_generic_writes_keep_distinct_calls_and_read_tools_remain_unjournaled(invocation_runtime):
    """通用写工具保留调用 ID 语义，明确只读标签的工具不进入写回执系统。"""
    repository, records = invocation_runtime
    calls = []

    async def perform(value: str):
        """记录通用工具的真实执行次数。"""
        calls.append(value)
        return json.dumps({"success": True})

    write = StructuredTool.from_function(coroutine=perform, name="generic_write", description="Write data", tags=["write"])
    read = StructuredTool.from_function(coroutine=perform, name="generic_read", description="Read data", tags=["read"])
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[
            _call("generic-1", {"value": "same"}, write.name),
            _call("generic-2", {"value": "same"}, write.name),
            _call("generic-read", {"value": "read"}, read.name),
        ]), AIMessage(content="完成"),
    ], tools=[write, read])
    await _invoke(graph)
    assert calls.count("same") == 2
    assert calls.count("read") == 1
    assert {record["invocation_id"] for record in records()} == {"generic-1", "generic-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "retry"), [
    (WRITE_ARGUMENTS, {**WRITE_ARGUMENTS, "body": {**WRITE_ARGUMENTS["body"], "allow_unrecognized": False}}),
    ({**WRITE_ARGUMENTS, "body": {**WRITE_ARGUMENTS["body"], "ignored_by_endpoint": "first"}}, WRITE_ARGUMENTS),
    (
        {"operation_id": "config.system.update", "body": {"setting_key": "PROJECT_NAME", "value": "desired"}},
        {"operation_id": "config.system.update", "body": {"setting_key": "PROJECT_NAME", "value": "desired", "operation": "replace"}},
    ),
    (
        {"operation_id": "plugin.install", "path_params": {"plugin_id": "Demo"}, "query": {"force": "false"}},
        {"operation_id": "plugin.install", "path_params": {"plugin_id": "Demo"}, "body": {"force": False, "repo_url": ""}},
    ),
])
async def test_api_effective_parameters_prevent_unknown_retry_bypass(invocation_runtime, monkeypatch, first, retry):
    """嵌套默认值、模型忽略字段和 GET 位置变化不能绕过未知写入防重。"""
    repository, records = invocation_runtime
    writes = []

    async def run(_self, operation_id, **kwargs):
        """记录实际执行参数，核验读取返回未匹配值使旧写入继续保持未知。"""
        if operation_id == "config.system.get":
            return json.dumps({"success": True, "data": {"settings": [{
                "setting_key": "PROJECT_NAME", "value": "not-yet-confirmed", "redacted": False,
            }]}})
        writes.append({"operation_id": operation_id, **kwargs})
        return json.dumps({"execution_outcome": "unknown"})

    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("initial", first)]), AIMessage(content="等待确认"),
        AIMessage(content="", tool_calls=[_call("retry", retry)]), AIMessage(content="先核验"),
    ])
    await _invoke(graph)
    result = await _invoke(graph, "继续刚才的操作")
    assert len(writes) == 1
    assert len(records()) == 1
    assert records()[0]["status"] == "unknown"
    assert json.loads(_tool_messages(result)[-1].content)["replayed"] is True
    tool = MoviePilotApiTool(session_id="canonical", user_id="owner")
    normalized = tool.canonical_arguments(first)
    assert normalized == tool.canonical_arguments(retry)
    assert writes[0] == normalized
    assert "ignored_by_endpoint" not in (normalized.get("body") or {})


@pytest.mark.asyncio
async def test_api_forbidden_query_keys_do_not_execute_or_claim(invocation_runtime, monkeypatch):
    """路径和查询合同禁止的字段在认领前拒绝，不能仅从指纹丢弃后仍传给服务端。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value='{"success":true}')
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    arguments = {"operation_id": "plugin.install", "path_params": {"plugin_id": "Demo"}, "query": {"unlisted": True}}
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("invalid", arguments)]), AIMessage(content="修正参数"),
    ])
    result = await _invoke(graph)
    assert _tool_messages(result)[0].status == "error"
    assert "输入合同校验" in _tool_messages(result)[0].content
    payload = json.loads(_tool_messages(result)[0].content)
    assert payload["operation_id"] == "plugin.install"
    assert "path_params" in payload["input_contract"]["allowed_arguments"]
    assert "query" in payload["input_contract"]["allowed_arguments"]
    assert records() == []
    run.assert_not_awaited()


def test_api_schema_normalization_preserves_free_setting_values_and_explicit_null():
    """自由字典和显式 null 保持原意，引用模型里的显式默认值仍可规范。"""
    tool = MoviePilotApiTool(session_id="canonical", user_id="owner")
    free_value = {"unknown_keys_are_business_data": {"force": "false"}, "optional": None}
    result = tool.canonical_arguments({"operation_id": "config.system.update", "body": {
        "setting_key": "CUSTOM_SETTING", "value": free_value, "match_field": None,
    }})
    assert result["body"]["value"] == free_value
    assert result["body"]["match_field"] is None
    assert result["body"]["operation"] == "replace"
    nested = tool.canonical_arguments({**WRITE_ARGUMENTS, "body": {"torrent_in": {
        **WRITE_ARGUMENTS["body"]["torrent_in"], "hit_and_run": "false", "grabs": "0",
    }}})
    assert nested["body"]["torrent_in"]["hit_and_run"] is False
    assert nested["body"]["torrent_in"]["grabs"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", [True, False])
async def test_boolean_resource_id_never_becomes_numeric_write_target(invocation_runtime, monkeypatch, resource_id):
    """布尔路径参数不能被规范成数字 ID，认领和真实删除都必须被阻止。"""
    repository, records = invocation_runtime
    run = AsyncMock(return_value='{"success":true}')
    monkeypatch.setattr(MoviePilotApiTool, "run", run)
    graph, _model, _middleware = _graph(repository, [
        AIMessage(content="", tool_calls=[_call("bad-id", {
            "operation_id": "subscription.delete", "path_params": {"subscribe_id": resource_id},
        })]), AIMessage(content="需要有效的订阅编号"),
    ])
    result = await _invoke(graph)
    assert _tool_messages(result)[0].status == "error"
    assert records() == []
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_read_tag_does_not_bypass_persistent_identity(invocation_runtime, monkeypatch):
    """外部 MCP 的固定 Read 标签不构成只读证明，同 ID 防重而新 ID 仍照常执行。"""
    repository, records = invocation_runtime
    spec = AgentMcpToolSpec(
        server=AgentMcpServerConfig(id="external", name="Remote", transport="stdio", command="unused"),
        name="operation", agent_tool_name="mcp_remote_operation", description="Remote operation",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )
    tools = await create_external_mcp_tools(session_id="invocation-chat", user_id="owner", specs=[spec])
    run = AsyncMock(return_value='{"success":true,"data":"remote-result"}')
    monkeypatch.setattr(McpExternalTool, "run", run)
    replies = []
    for call_id in ("same-call", "same-call", "new-call"):
        graph, _model, _middleware = _graph(repository, [
            AIMessage(content="", tool_calls=[_call(call_id, {"value": "same"}, tools[0].name)]),
            AIMessage(content="已核验"),
        ], tools=tools)
        replies.append(json.loads(_tool_messages(await _invoke(graph))[0].content))
    assert run.await_count == 2
    assert len(records()) == 2
    assert replies[0]["data"] == replies[2]["data"] == "remote-result"
    assert replies[1]["replayed"] is True
