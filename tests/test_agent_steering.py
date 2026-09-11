"""运行中 Agent 消息排队、真实 HumanMessage 注入及身份隔离测试。"""

import asyncio
import json

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.middleware.steering import SteeringMiddleware
from app.agent.session import AgentSessionOwner
from app.agent.steering import SteeringInbox, bind_steering_inbox, reset_steering_inbox


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
