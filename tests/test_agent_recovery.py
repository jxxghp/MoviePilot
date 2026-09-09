"""Agent 中断后恢复已确认事实，避免重复执行有副作用的工具。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.memory import MemoryManager
from app.agent.middleware.patching import PatchToolCallsMiddleware
from app.agent.middleware.plan import PLAN_SNAPSHOT_KEY
from app.agent.orchestrator import MoviePilotAgent


class _InterruptedGraph:
    """在图已保存工具状态后失败或等待取消的执行替身。"""

    def __init__(self, messages, *, blocked=False, state_error=False):
        """保存可恢复图状态和由用例控制的故障模式。"""
        self.messages = messages
        self.blocked = blocked
        self.state_error = state_error
        self.started = asyncio.Event()

    async def ainvoke(self, _payload, config=None):
        """通知工具执行已结束，再模拟下一次模型调用中断。"""
        self.started.set()
        if self.blocked:
            await asyncio.Event().wait()
        raise RuntimeError("model request failed")

    async def astream(self, payload, **_kwargs):
        """保持与真实流式图相同的异常传播边界。"""
        await self.ainvoke(payload)
        yield {}

    def get_state(self, _config):
        """提供已完成工具回执，或模拟 checkpoint 不可读。"""
        if self.state_error:
            raise RuntimeError("checkpoint unavailable")
        return SimpleNamespace(values={"messages": self.messages})


@pytest.fixture
def recovery_agent(monkeypatch):
    """隔离模型、持久化和通知边界，仅保留真实会话记忆缓存。"""
    persistence = SimpleNamespace(async_save_agent_messages=AsyncMock())
    memory = MemoryManager(persistence=persistence)
    agent = MoviePilotAgent(
        session_id="recovery-session",
        user_id="10001",
        channel="WebAgent",
        source="web",
        memory=memory,
    )
    monkeypatch.setattr(agent, "_should_stream", lambda: False)
    monkeypatch.setattr(agent, "_dispatch_execution_notice", AsyncMock())
    monkeypatch.setattr(agent, "_send_agent_tokens_usage_event", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_save_assistant_display_message_once", AsyncMock())
    monkeypatch.setattr(agent, "send_agent_message", AsyncMock())
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, "")),
        set_dispatch_policy=lambda **_kwargs: None,
        start_streaming=AsyncMock(),
    )
    return agent, persistence


def _completed_tool_messages():
    """构造已创建下载、另一个写操作尚无回执的会话。"""
    return [
        HumanMessage(content="下载电影并更新订阅"),
        AIMessage(
            content="开始处理",
            tool_calls=[
                {"id": "download-call", "name": "download", "args": {"media_id": "42"}},
                {"id": "subscription-call", "name": "subscribe", "args": {"media_id": "42"}},
            ],
        ),
        ToolMessage(content='{"download_id": "already-created"}', tool_call_id="download-call"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_failure_preserves_completed_receipts_and_marks_unknown_outcomes(recovery_agent, streaming):
    """失败后的新一轮输入应包含已完成回执，悬空写调用保持结果未知。"""
    agent, persistence = recovery_agent
    agent._should_stream = lambda: streaming
    original = _completed_tool_messages()
    graph = _InterruptedGraph(original)
    agent._create_agent = AsyncMock(return_value=graph)
    agent._compiled_agent_bundle = object()

    result, _ = await agent._execute_agent(original[:1])

    assert result == "智能助手执行失败，请稍后重试"
    assert agent._compiled_agent_bundle is None
    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    receipts = [message for message in recovered if isinstance(message, ToolMessage)]
    assert receipts[0].content == original[2].content
    assert receipts[0].status == "success"
    assert receipts[1].tool_call_id == "subscription-call"
    assert receipts[1].status == "error"
    assert "unknown" in receipts[1].content.lower()
    assert "read-only" in receipts[1].content.lower()
    assert not recovered[-1].tool_calls
    assert len(original) == 3
    persistence.async_save_agent_messages.assert_awaited_once()

    agent._execute_agent = AsyncMock(return_value=("继续核验状态", {}))
    agent.prepare_chat_title = AsyncMock()
    agent._save_display_history_messages = AsyncMock()
    await agent.process("继续")
    next_messages = agent._execute_agent.call_args.args[0]
    assert next_messages[:-1] == recovered
    assert json.loads(next_messages[-1].content[0]["text"])["message"] == "继续"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_preserves_snapshot_and_still_propagates(recovery_agent, streaming):
    """用户取消必须保存完成事实，同时保持调用方收到 CancelledError。"""
    agent, persistence = recovery_agent
    agent._should_stream = lambda: streaming
    graph = _InterruptedGraph(_completed_tool_messages(), blocked=True)
    agent._create_agent = AsyncMock(return_value=graph)
    agent._compiled_agent_bundle = object()
    execution = asyncio.create_task(agent._execute_agent(graph.messages[:1]))
    await graph.started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert agent._compiled_agent_bundle is None
    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    assert "already-created" in str(messages_to_dict(recovered))
    assert "取消" in recovered[-1].content
    persistence.async_save_agent_messages.assert_awaited_once()
    agent.stream_handler.stop_streaming.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_sanitizes_checkpoint_without_mutating_live_messages(recovery_agent):
    """中断快照清理凭据和供应商载荷，不把工具 artifact 或异常体落盘。"""
    agent, persistence = recovery_agent
    original = _completed_tool_messages()
    original[0].content = "api_key=raw-user-secret"
    original[1].tool_calls[0]["args"]["password"] = "raw-tool-secret"
    original[1].additional_kwargs = {"raw_provider_payload": "private-provider-body"}
    original[2].content = '{"download_id": "already-created", "token": "raw-result-secret"}'
    original[2].artifact = {"payload": "private-artifact"}
    original[2].response_metadata = {"payload": "private-metadata"}
    agent._create_agent = AsyncMock(return_value=_InterruptedGraph(original))

    await agent._execute_agent(original[:1])

    saved = json.dumps(persistence.async_save_agent_messages.call_args.kwargs["messages"])
    assert "already-created" in saved
    for secret in (
        "raw-user-secret", "raw-tool-secret", "raw-result-secret",
        "private-provider-body", "private-artifact", "private-metadata",
    ):
        assert secret not in saved
    assert original[1].tool_calls[0]["args"]["password"] == "raw-tool-secret"
    assert original[2].artifact == {"payload": "private-artifact"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checkpoint", "persistence", "creation", "configuration"])
async def test_snapshot_failure_does_not_replace_original_error(recovery_agent, failure):
    """读取或保存恢复快照失败时，仍按原来的执行错误收尾。"""
    agent, _persistence = recovery_agent
    graph = _InterruptedGraph(_completed_tool_messages(), state_error=failure == "checkpoint")
    agent._create_agent = AsyncMock(return_value=graph)
    if failure == "creation":
        agent._create_agent.side_effect = RuntimeError("creation failed")
    if failure == "configuration":
        agent._get_recursion_limit = lambda: int("invalid configuration")
    if failure == "persistence":
        agent._memory.async_save_agent_messages = AsyncMock(side_effect=RuntimeError("save failed"))
    agent._compiled_agent_bundle = object()

    result, _ = await agent._execute_agent(graph.messages[:1])

    assert result == "智能助手执行失败，请稍后重试"
    assert agent._compiled_agent_bundle is None
    agent.stream_handler.stop_streaming.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_does_not_create_chat_history_for_background_tasks(recovery_agent):
    """无渠道后台任务沿用不保存普通会话的边界。"""
    agent, persistence = recovery_agent
    agent.channel = None
    agent.source = None
    agent._create_agent = AsyncMock(return_value=_InterruptedGraph(_completed_tool_messages()))

    await agent._execute_agent([])

    persistence.async_save_agent_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_timeout_keeps_memory_and_does_not_delay_cancellation(recovery_agent, monkeypatch):
    """持久化等待有界，超时后内存事实仍可复用且取消照常收敛。"""
    agent, persistence = recovery_agent
    graph = _InterruptedGraph(_completed_tool_messages(), blocked=True)
    agent._create_agent = AsyncMock(return_value=graph)
    monkeypatch.setattr("app.agent.orchestrator.AGENT_RECOVERY_SNAPSHOT_TIMEOUT", 0.01)

    async def block_save(**_kwargs):
        """模拟数据库写边界迟迟未返回，不访问真实数据库。"""
        await asyncio.Event().wait()

    persistence.async_save_agent_messages.side_effect = block_save
    execution = asyncio.create_task(agent._execute_agent(graph.messages[:1]))
    await graph.started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=1)

    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    assert "already-created" in str(messages_to_dict(recovered))


@pytest.mark.asyncio
async def test_second_cancellation_during_snapshot_still_invalidates_graph(recovery_agent):
    """保存中断快照时再次取消，也不能遗留旧图让后续请求重用。"""
    agent, persistence = recovery_agent
    graph = _InterruptedGraph(_completed_tool_messages(), blocked=True)
    agent._create_agent = AsyncMock(return_value=graph)
    agent._compiled_agent_bundle = object()
    saving = asyncio.Event()

    async def block_save(**_kwargs):
        """将第二次取消定位到持久化等待阶段。"""
        saving.set()
        await asyncio.Event().wait()

    persistence.async_save_agent_messages.side_effect = block_save
    execution = asyncio.create_task(agent._execute_agent(graph.messages[:1]))
    await graph.started.wait()
    execution.cancel()
    await asyncio.wait_for(saving.wait(), timeout=1)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert agent._compiled_agent_bundle is None
    agent.stream_handler.stop_streaming.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupted_snapshot_preserves_sanitized_plan(recovery_agent):
    """图中的未完成计划随中断提示写入快照，凭据不会进入计划恢复数据。"""
    agent, persistence = recovery_agent
    graph = _InterruptedGraph(_completed_tool_messages())
    plan = {
        "objective": "处理下载",
        "steps": [{"step": "核验任务", "status": "in_progress", "evidence": ""}],
        "explanation": "password=private-plan-secret",
    }
    graph.get_state = lambda _config: SimpleNamespace(values={"messages": graph.messages, "task_plan": plan})
    agent._create_agent = AsyncMock(return_value=graph)

    await agent._execute_agent(graph.messages[:1])

    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    snapshot = recovered[-1].additional_kwargs[PLAN_SNAPSHOT_KEY]
    assert snapshot["steps"][0]["status"] == "in_progress"
    assert "private-plan-secret" not in str(snapshot)
    assert "private-plan-secret" not in str(persistence.async_save_agent_messages.call_args)
    assert plan["explanation"] == "password=private-plan-secret"


class _InterruptedModel(FakeMessagesListChatModel):
    """允许首次工具执行后模型失败，再以同样的历史继续回应。"""

    fail_after_tool: bool = True

    def bind_tools(self, _tools, **_kwargs):
        """测试模型固定返回工具协议消息，不需要连接真实供应商。"""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """第二次模型请求只失败一次，保留已完成工具所在的 checkpoint。"""
        if self.i == 1 and self.fail_after_tool:
            self.fail_after_tool = False
            raise RuntimeError("model disconnected after download")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.asyncio
async def test_real_graph_rebuild_uses_receipts_without_replaying_completed_tool(recovery_agent):
    """真实 LangGraph 中工具已成功、后续模型失败时，重建图不重放旧工具。"""
    agent, _persistence = recovery_agent
    writes = []

    @tool
    def add_download() -> str:
        """记录一次不可重复创建的测试下载。"""
        writes.append("download-created")
        return '{"download_id": "created-once"}'

    model = _InterruptedModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "add-once", "name": "add_download", "args": {}}]),
        AIMessage(content="已核验之前创建的下载"),
    ])
    graphs = [
        create_agent(
            model=model,
            tools=[add_download],
            middleware=[PatchToolCallsMiddleware()],
            checkpointer=InMemorySaver(),
        )
        for _ in range(2)
    ]
    agent._create_agent = AsyncMock(side_effect=graphs)
    first_message = HumanMessage(content="创建一个下载")

    await agent._execute_agent([first_message])
    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    assert any(isinstance(message, ToolMessage) and "created-once" in message.content for message in recovered)
    await agent._execute_agent([*recovered, HumanMessage(content="继续核验")])

    assert writes == ["download-created"]
    assert agent._streamed_output == "已核验之前创建的下载"


@pytest.mark.asyncio
async def test_parallel_tool_failure_keeps_successful_pending_write(recovery_agent):
    """同批并行工具部分失败时，checkpoint 的成功 pending write 仍进入恢复历史。"""
    agent, _persistence = recovery_agent
    completed = asyncio.Event()
    writes = []

    @tool
    async def add_download() -> str:
        """模拟已生效的下载写操作，并通知并行失败工具。"""
        writes.append("download-created")
        completed.set()
        return '{"download_id": "parallel-created-once"}'

    @tool
    async def update_subscription() -> str:
        """等待下载生效后模拟另一路写操作失败。"""
        await completed.wait()
        raise RuntimeError("subscription connection lost")

    model = _InterruptedModel(fail_after_tool=False, responses=[
        AIMessage(content="", tool_calls=[
            {"id": "parallel-download", "name": "add_download", "args": {}},
            {"id": "parallel-subscription", "name": "update_subscription", "args": {}},
        ]),
        AIMessage(content="下载已有成功回执，继续核验订阅状态"),
    ])
    graphs = [
        create_agent(
            model=model,
            tools=[add_download, update_subscription],
            middleware=[PatchToolCallsMiddleware()],
            checkpointer=InMemorySaver(),
        )
        for _ in range(2)
    ]
    agent._create_agent = AsyncMock(side_effect=graphs)

    await agent._execute_agent([HumanMessage(content="下载并更新订阅")])
    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    receipts = {message.tool_call_id: message for message in recovered if isinstance(message, ToolMessage)}
    assert receipts["parallel-download"].status == "success"
    assert "parallel-created-once" in receipts["parallel-download"].content
    assert receipts["parallel-subscription"].status == "error"
    assert "outcome is unknown" in receipts["parallel-subscription"].content

    await agent._execute_agent([*recovered, HumanMessage(content="继续")])

    assert writes == ["download-created"]
    assert agent._streamed_output == "下载已有成功回执，继续核验订阅状态"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["interleaved", "gemini", "anthropic", "responses"])
async def test_signed_history_recovers_as_portable_facts_without_broken_protocol(recovery_agent, provider):
    """签名或思考工具链转为普通历史事实，供应商 SDK 可重新组装下一轮请求。"""
    from langchain_anthropic.chat_models import _format_messages
    from langchain_google_genai.chat_models import _parse_chat_history
    from langchain_openai.chat_models.base import _convert_message_to_dict

    agent, persistence = recovery_agent
    original = _completed_tool_messages()
    if provider == "interleaved":
        original[1].additional_kwargs = {"reasoning_content": "private-reasoning-body"}
    elif provider == "gemini":
        original[1].additional_kwargs = {
            "__gemini_function_call_thought_signatures__": {"download-call": "opaque-signature"},
        }
    elif provider == "anthropic":
        original[1].content = [
            {"type": "thinking", "thinking": "private-reasoning-body", "signature": "opaque-signature"},
            {"type": "text", "text": "已开始执行"},
        ]
    else:
        original[1].content = [
            {"type": "reasoning", "reasoning": "private-reasoning-body", "extras": {"signature": "opaque-signature"}},
            {"type": "text", "text": "已开始执行"},
        ]
        original[1].response_metadata = {"model_provider": "openai", "output_version": "v1"}
    plan = {"objective": "完成下载和订阅", "steps": [{"step": "核验订阅", "status": "pending"}]}
    graph = _InterruptedGraph(original)
    graph.get_state = lambda _config: SimpleNamespace(values={"messages": original, "task_plan": plan})
    agent._create_agent = AsyncMock(return_value=graph)

    await agent._execute_agent(original[:1])

    recovered = await agent._memory.async_get_agent_messages(agent.session_id, agent.user_id)
    saved = str(persistence.async_save_agent_messages.call_args)
    assert recovered[0].type == "human"
    assert recovered[0].content == original[0].content
    assert recovered[-1].additional_kwargs[PLAN_SNAPSHOT_KEY]["objective"] == plan["objective"]
    assert not any(isinstance(message, ToolMessage) or getattr(message, "tool_calls", []) for message in recovered)
    assert "already-created" in saved
    assert "subscription-call" in saved
    assert "outcome is unknown" in saved
    assert '"status": "success"' in saved
    assert '"status": "error"' in saved
    assert "private-reasoning-body" not in saved
    assert "opaque-signature" not in saved
    assert "仅作观察数据" in saved

    next_messages = [*recovered, HumanMessage(content="继续")]
    openai_payload = [_convert_message_to_dict(message) for message in next_messages]
    _system, anthropic_payload = _format_messages(next_messages)
    _system, gemini_payload = _parse_chat_history(next_messages, model="gemini-3-pro")
    assert openai_payload[-1]["role"] == "user"
    assert anthropic_payload[-1]["role"] == "user"
    assert gemini_payload[-1].role == "user"


def test_portable_recovery_retains_historical_plan_before_state_restore():
    """计划中间件恢复前即失败时，历史消息携带的计划仍可随事实快照恢复。"""
    messages = _completed_tool_messages()
    messages[1].additional_kwargs[PLAN_SNAPSHOT_KEY] = {
        "objective": "核验下载和订阅",
        "steps": [{"step": "检查订阅状态", "status": "pending"}],
    }

    recovered = MoviePilotAgent._portable_recovery_messages(messages)

    assert recovered[-1].additional_kwargs[PLAN_SNAPSHOT_KEY]["objective"] == "核验下载和订阅"
    assert "already-created" in recovered[-1].content
