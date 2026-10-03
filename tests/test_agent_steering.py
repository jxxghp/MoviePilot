"""运行中 Agent 消息排队、真实 HumanMessage 注入及身份隔离测试。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.middleware.steering import SteeringMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.session import AgentSessionOwner, _MessageTask
from app.agent.steering import STEERING_QUEUE_MAX_SIZE, SteeringInbox, bind_steering_inbox, reset_steering_inbox
from app.schemas.types import NotificationChannel, ReplyMode


@pytest.mark.asyncio
async def test_steering_message_is_injected_as_real_human_message() -> None:
    """下一次模型调用应在图状态中看到真实 HumanMessage，而不是提示词拼接。"""
    inbox = SteeringInbox("session", "user")
    await inbox.begin_run()
    queued = await inbox.enqueue(user_id="user", text="补充要求")
    assert queued is not None

    graph = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="完成")])),
        tools=[],
        middleware=[SteeringMiddleware()],
        checkpointer=InMemorySaver(),
    )
    token = bind_steering_inbox(inbox)
    try:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="开始任务")]},
            config={"configurable": {"thread_id": "session"}},
        )
    finally:
        reset_steering_inbox(token)

    messages = result["messages"]
    assert [message.type for message in messages] == ["human", "human", "ai"]
    assert "补充要求" in messages[1].content[0]["text"]
    assert messages[1].additional_kwargs["moviepilot_steering_message_id"] == queued.message_id
    steering_payload = json.loads(messages[1].content[0]["text"])
    assert "保留此前的范围" in steering_payload["continuation_context"]


@pytest.mark.asyncio
async def test_steering_does_not_break_unresolved_tool_call_pairing() -> None:
    """未交付工具调用所在的状态边界不应插入用户消息。"""
    inbox = SteeringInbox("session", "user")
    await inbox.begin_run()
    assert await inbox.enqueue(user_id="user", text="稍等") is not None
    middleware = SteeringMiddleware()
    token = bind_steering_inbox(inbox)
    seen_messages = []

    async def handler(request):
        """记录未注入的模型请求，模拟下游模型处理。"""
        seen_messages.extend(request.messages)
        return ModelResponse(result=[AIMessage(content="继续执行工具")])

    try:
        pending_tool_message = AIMessage(
            content="",
            tool_calls=[{"name": "tool", "args": {}, "id": "call"}],
        )
        await middleware.awrap_model_call(
            request=ModelRequest(
                model=GenericFakeChatModel(messages=iter([AIMessage(content="unused")])),
                tools=[],
                system_message=None,
                response_format=None,
                messages=[pending_tool_message],
                tool_choice=None,
                state={"messages": [pending_tool_message]},
                runtime=None,
            ),
            handler=handler,
        )
    finally:
        reset_steering_inbox(token)
    assert seen_messages[-1].tool_calls
    assert inbox.pending_count == 1


@pytest.mark.asyncio
async def test_owner_routes_matching_user_message_to_active_steering_inbox() -> None:
    """活动会话只接受同一用户的补充消息，其他用户回退到普通入口。"""
    started = asyncio.Event()
    release = asyncio.Event()
    processed_messages: list[str] = []

    class BlockingAgent:
        """模拟持久会话 Agent，暴露运行边界供 owner 测试。"""

        def __init__(self, **kwargs):
            """保存宿主注入字段。"""
            self.__dict__.update(kwargs)
            self.user_id = str(kwargs["user_id"])

        def set_output_callback(self, callback):
            """兼容会话 owner 的输出回调更新。"""
            self.output_callback = callback

        def set_protected_output_callback(self, callback):
            """兼容会话 owner 的敏感输出回调更新。"""
            self.protected_output_callback = callback

        def get_session_status(self):
            """返回 owner 状态合并所需的最小快照。"""
            return {"model": "fake", "is_processing": True}

        async def process(self, message, **kwargs):
            """阻塞到测试释放，模拟长时间模型运行。"""
            processed_messages.append(message)
            started.set()
            await release.wait()
            return message

        async def cleanup(self):
            """报告测试 Agent 已收敛。"""
            return True

    owner = AgentSessionOwner()
    owner._accepting_tasks = True
    running = asyncio.create_task(
        owner.process_message(
            session_id="session",
            user_id="user",
            message="开始",
            agent_factory=BlockingAgent,
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    accepted = await owner.submit_steering_message(
        session_id="session",
        user_id="user",
        message="继续检查",
    )
    rejected = await owner.submit_steering_message(
        session_id="session",
        user_id="other",
        message="越权",
    )
    assert accepted is not None
    assert rejected is None
    assert owner.get_session_status("session")["steering_pending"] == 1

    release.set()
    assert await asyncio.wait_for(running, 1) == "继续检查"
    assert processed_messages == ["开始", "继续检查"]
    worker = owner._session_workers.get("session")
    if worker:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_steering_finish_boundary_returns_pending_messages_once() -> None:
    """运行收尾与入队共享锁时，未注入消息只能被交给下一轮一次。"""
    inbox = SteeringInbox("session", "user")
    await inbox.begin_run()
    message = await inbox.enqueue(user_id="user", text="收尾前到达")
    assert message is not None
    pending = await inbox.finish_run()
    assert pending == (message,)
    assert await inbox.finish_run() == ()
    assert await inbox.enqueue(user_id="user", text="已关闭运行") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [NotificationChannel.Telegram, NotificationChannel.Feishu, NotificationChannel.Wechat])
async def test_channel_followup_enters_running_graph_without_starting_another_task(monkeypatch, channel) -> None:
    """真实会话入口应把连续补充消息注入当前图，并仅在消费时切分渠道回复。"""
    started = asyncio.Event()
    release = asyncio.Event()
    results = []
    processed = []
    monkeypatch.setattr("app.agent.session._async_start_processing_status", AsyncMock(return_value=None))
    monkeypatch.setattr("app.agent.session._async_finish_processing_status", AsyncMock())

    class ChannelAgent:
        """使用真实 steering 中间件，隔离模型与消息渠道的测试 Agent。"""

        def __init__(self, **kwargs):
            """接收会话上下文并记录消息切分调用。"""
            self.__dict__.update(kwargs)
            self.stream_handler = Mock()
            self._tool_context = {"user_reply_sent": True}

        def start_steering_reply(self, original_message_id=None):
            """复用生产回复切分，验证补充输入解除前段主动回复的抑制状态。"""
            MoviePilotAgent.start_steering_reply(self, original_message_id)

        async def process(self, message, **_kwargs):
            """暂停当前任务，让补充输入在下一次真实模型调用前进入 inbox。"""
            processed.append(message)
            started.set()
            await release.wait()
            graph = create_agent(
                model=GenericFakeChatModel(messages=iter([AIMessage(content="已按补充要求完成")])),
                tools=[],
                middleware=[SteeringMiddleware()],
                checkpointer=InMemorySaver(),
            )
            token = bind_steering_inbox(self.steering_inbox)
            try:
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=message), AIMessage(content="正在检查")]},
                    config={"configurable": {"thread_id": "channel-session"}},
                )
                results.extend(result["messages"])
            finally:
                reset_steering_inbox(token)
            return "完成"

    owner = AgentSessionOwner()
    owner._accepting_tasks = True
    context = {
        "session_id": "channel-session", "user_id": "user", "channel": channel.value,
        "source": "configured-channel", "original_chat_id": "chat", "is_channel_admin": True,
    }
    try:
        await owner.process_message(**context, message="检查服务", agent_factory=ChannelAgent)
        await asyncio.wait_for(started.wait(), 1)
        agent = owner.active_agents["channel-session"]
        await owner.process_message(
            **context, message="补充检查目录", original_message_id="user-2",
            images=["data:image/png;base64,test"], files=[{"ref": "log.txt"}],
        )
        await owner.process_message(**context, message="仅报告异常", original_message_id="user-3")
        assert owner._session_queues["channel-session"].empty()
        assert agent.steering_inbox.pending_count == 2
        agent.stream_handler.start_new_message.assert_not_called()
        release.set()
        await asyncio.wait_for(owner._session_queues["channel-session"].join(), 2)

        assert processed == ["检查服务"]
        assert [message.type for message in results] == ["human", "ai", "human", "human", "ai"]
        payload = json.loads(results[2].content[0]["text"])
        assert payload["message"] == "补充检查目录"
        assert payload["files"] == [{"ref": "log.txt"}]
        assert results[2].content[1]["image_url"]["url"] == "data:image/png;base64,test"
        assert json.loads(results[3].content[0]["text"])["message"] == "仅报告异常"
        assert [call.args for call in agent.stream_handler.start_new_message.call_args_list] == [("user-2",), ("user-3",)]
        assert agent.original_message_id == "user-3"
        assert agent._tool_context["user_reply_sent"] is False
    finally:
        release.set()
        worker = owner._session_workers.get("channel-session")
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"user_id": "another-user"}, {"source": "another-bot"}, {"original_chat_id": "another-chat"},
    {"is_channel_admin": False}, {"channel": NotificationChannel.Feishu.value},
    {"channel": NotificationChannel.WebAgent.value}, {"reply_mode": ReplyMode.CAPTURE_ONLY},
    {"allow_message_tools": False}, {"scheduled_run_id": "scheduled"},
])
async def test_channel_steering_preserves_identity_and_independent_task_boundaries(changed) -> None:
    """其它用户、聊天、渠道、权限或独立调用不得进入已有渠道任务的上下文。"""
    context = {
        "session_id": "session", "user_id": "user", "channel": NotificationChannel.Telegram.value,
        "source": "bot", "original_chat_id": "chat", "is_channel_admin": True,
    }
    owner = AgentSessionOwner()
    owner._accepting_tasks = True
    inbox = SteeringInbox("session", "user")
    await inbox.begin_run()
    owner._session_active_tasks["session"] = _MessageTask(**context, message="开始")
    owner._session_steering_inboxes["session"] = inbox
    owner._session_queues["session"] = asyncio.Queue()
    owner._session_workers["session"] = Mock(done=lambda: False)
    await owner.process_message(**{**context, **changed}, message="独立请求")
    assert inbox.pending_count == 0
    assert owner._session_queues["session"].get_nowait().message == "独立请求"


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_channel_steering_falls_back_to_queue_when_full_or_finished(running) -> None:
    """inbox 满或运行已收尾时，补充消息仍进入普通队列且只保留一次。"""
    context = {
        "session_id": "session", "user_id": "user", "channel": NotificationChannel.Telegram.value,
        "source": "bot",
    }
    owner = AgentSessionOwner()
    owner._accepting_tasks = True
    inbox = SteeringInbox("session", "user")
    if running:
        await inbox.begin_run()
        for index in range(STEERING_QUEUE_MAX_SIZE):
            await inbox.enqueue(user_id="user", text=str(index))
    owner._session_active_tasks["session"] = _MessageTask(**context, message="开始")
    owner._session_steering_inboxes["session"] = inbox
    owner._session_queues["session"] = asyncio.Queue()
    owner._session_workers["session"] = Mock(done=lambda: False)
    await owner.process_message(**context, message="不能丢失")
    assert inbox.pending_count == (STEERING_QUEUE_MAX_SIZE if running else 0)
    assert owner._session_queues["session"].get_nowait().message == "不能丢失"
