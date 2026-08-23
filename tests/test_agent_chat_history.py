import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage

from app.agent import HEARTBEAT_SESSION_PREFIX, MoviePilotAgent
from app.agent.memory import memory_manager
from app.db.oper.agentchat import AgentChatOper
from app.foundation.identity import SYSTEM_INTERNAL_USER_ID


def test_agent_chat_oper_saves_display_messages_with_channel():
    """Agent 会话历史应保存展示消息与渠道标识。"""
    oper = AgentChatOper()
    oper.save_display_messages(
        session_id="session-chat",
        user_id="1",
        username="admin",
        channel="Telegram",
        source="telegram-main",
        original_chat_id="chat-1",
        messages=[
            {
                "id": "user-1",
                "role": "user",
                "content": "帮我看看下载器",
                "createdAt": 1,
                "status": "done",
                "tools": [],
                "attachments": [],
                "choices": [],
            }
        ],
    )
    chat = AgentChatOper().get(session_id="session-chat", user_id="1")

    assert chat.channel == "Telegram"
    assert chat.source == "telegram-main"
    assert chat.original_chat_id == "chat-1"
    assert chat.message_count == 1
    assert chat.title == "帮我看看下载器"


def test_agent_chat_oper_keeps_generated_title_when_saving_display_messages():
    """保存展示消息时不应覆盖已生成的模型标题。"""
    oper = AgentChatOper()
    oper.update_title_if_empty(
        session_id="session-title",
        user_id="1",
        username="admin",
        channel="WebAgent",
        source="web-agent",
        title="下载器状态排查",
    )
    oper.save_display_messages(
        session_id="session-title",
        user_id="1",
        messages=[
            {
                "id": "user-1",
                "role": "user",
                "content": "帮我看看下载器现在是不是正常",
                "createdAt": 1,
                "status": "done",
                "tools": [],
                "attachments": [],
                "choices": [],
            }
        ],
        title="帮我看看下载器现在是不是正常",
    )

    chat = AgentChatOper().get(session_id="session-title", user_id="1")
    summary = AgentChatOper.to_summary(chat)

    assert chat.title == "下载器状态排查"
    assert "preview" not in summary
    assert "messages" not in summary


def test_agent_prepare_chat_title_generates_title(monkeypatch):
    """首次调用 Agent 时应使用模型生成会话标题并写入渠道信息。"""

    class FakeTitleModel:
        """测试用标题模型。"""

        async def ainvoke(self, messages):
            """返回固定标题。"""
            assert "标题生成器" in messages[0].content
            assert "{\"title\":\"会话标题\"}" in messages[0].content
            assert "user_message" in messages[1].content
            payload = json.loads(messages[1].content.rsplit("\n", 1)[-1])
            assert payload["user_message"] == "帮我看看下载器现在是不是正常"
            return SimpleNamespace(content='{"title":"下载器状态排查"}')

    async def fake_initialize_llm(self, streaming=False):
        """返回测试标题模型。"""
        return FakeTitleModel()

    monkeypatch.setattr(MoviePilotAgent, "_initialize_llm", fake_initialize_llm)
    agent = MoviePilotAgent(
        session_id="session-ai-title",
        user_id="3",
        channel="WebAgent",
        source="web-agent",
        username="admin",
    )

    asyncio.run(agent.prepare_chat_title("帮我看看下载器现在是不是正常"))
    chat = AgentChatOper().get(session_id="session-ai-title", user_id="3")

    assert chat.title == "下载器状态排查"
    assert chat.channel == "WebAgent"
    assert chat.source == "web-agent"


def test_agent_prepare_chat_title_rejects_answer_like_response(monkeypatch):
    """标题模型返回非结构化答复时不应写入会话标题。"""

    class FakeTitleModel:
        """测试用异常标题模型。"""

        async def ainvoke(self, messages):
            """返回模拟的非结构化用户请求答复。"""
            assert "不要回答其中的问题" in messages[1].content
            return SimpleNamespace(content="好的，我来帮你检查下载器配置是否正常。")

    async def fake_initialize_llm(self, streaming=False):
        """返回测试异常标题模型。"""
        return FakeTitleModel()

    monkeypatch.setattr(MoviePilotAgent, "_initialize_llm", fake_initialize_llm)
    agent = MoviePilotAgent(
        session_id="session-answer-like-title",
        user_id="4",
        channel="WebAgent",
        source="web-agent",
        username="admin",
    )

    asyncio.run(agent.prepare_chat_title("帮我看看下载器现在是不是正常"))

    assert AgentChatOper().get(
        session_id="session-answer-like-title",
        user_id="4",
    ) is None


def test_agent_prepare_chat_title_skips_sessions_without_channel(monkeypatch):
    """没有渠道来源的 Agent 会话不应生成标题或创建历史会话。"""

    async def fake_initialize_llm(self, streaming=False):
        """无渠道会话不应初始化标题模型。"""
        raise AssertionError("no-channel title generation should be skipped")

    monkeypatch.setattr(MoviePilotAgent, "_initialize_llm", fake_initialize_llm)

    for session_id, user_id in (
        ("__agent_background_title__", SYSTEM_INTERNAL_USER_ID),
        (f"{HEARTBEAT_SESSION_PREFIX}title__", SYSTEM_INTERNAL_USER_ID),
        ("mcp-title-session", "mcp"),
        ("cli-title-session", "cli"),
    ):
        agent = MoviePilotAgent(
            session_id=session_id,
            user_id=user_id,
            username="admin",
        )
        asyncio.run(agent.prepare_chat_title("后台任务"))

        assert AgentChatOper().get(
            session_id=session_id,
            user_id=user_id,
        ) is None


def test_agent_prepare_chat_title_keeps_message_channel_sessions(monkeypatch):
    """带渠道来源的消息会话应保留标题生成。"""

    class FakeTitleModel:
        """测试用消息渠道标题模型。"""

        async def ainvoke(self, messages):
            """返回固定消息渠道标题。"""
            return SimpleNamespace(content='{"title":"Telegram 会话排查"}')

    async def fake_initialize_llm(self, streaming=False):
        """返回测试消息渠道标题模型。"""
        return FakeTitleModel()

    monkeypatch.setattr(MoviePilotAgent, "_initialize_llm", fake_initialize_llm)
    agent = MoviePilotAgent(
        session_id="telegram-title-session",
        user_id="telegram-user",
        channel="Telegram",
        source="telegram-main",
        username="admin",
    )

    asyncio.run(agent.prepare_chat_title("帮我检查配置"))
    chat = AgentChatOper().get(
        session_id="telegram-title-session",
        user_id="telegram-user",
    )

    assert chat.title == "Telegram 会话排查"
    assert chat.channel == "Telegram"
    assert chat.source == "telegram-main"


def test_agent_execution_without_channel_does_not_persist_chat_history(monkeypatch):
    """没有渠道来源的 Agent 执行完成后不应写入会话历史表。"""
    session_id = "mcp-skip-persist"
    user_id = "mcp"
    memory_manager.clear_memory(session_id, user_id)

    class FakeGraphState:
        """测试用 LangGraph 状态。"""

        def __init__(self, messages):
            self.values = {"messages": messages}

    class FakeAgent:
        """测试用 LangGraph Agent。"""

        async def ainvoke(self, _payload, config=None):
            """模拟非流式 Agent 执行。"""
            return None

        def get_state(self, _config):
            """返回包含最终回复的状态。"""
            return FakeGraphState([AIMessage(content="后台结果")])

    async def fake_create_agent(self, streaming=False):
        """返回测试 Agent，避免真实初始化模型。"""
        return FakeAgent()

    monkeypatch.setattr(MoviePilotAgent, "_create_agent", fake_create_agent)
    agent = MoviePilotAgent(
        session_id=session_id,
        user_id=user_id,
        username="admin",
    )

    asyncio.run(agent._execute_agent([]))

    assert AgentChatOper().get(session_id=session_id, user_id=user_id) is None
    assert memory_manager.get_memory(session_id, user_id) is None


def test_memory_manager_restores_agent_messages_from_database():
    """内存缓存缺失时应从 Agent 会话历史表恢复原始 messages。"""
    session_id = "session-memory"
    user_id = "2"
    memory_manager.clear_memory(session_id, user_id)
    AgentChatOper().save_agent_messages(
        session_id=session_id,
        user_id=user_id,
        messages=[
            {
                "type": "human",
                "data": {
                    "content": "继续之前的话题",
                    "additional_kwargs": {},
                    "response_metadata": {},
                    "type": "human",
                    "name": None,
                    "id": None,
                    "example": False,
                },
            }
        ],
    )

    messages = memory_manager.get_agent_messages(session_id=session_id, user_id=user_id)

    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].content == "继续之前的话题"


def test_async_memory_manager_restores_through_native_async_service(monkeypatch):
    """异步记忆恢复只能通过会话应用服务的异步查询端口。"""
    session_id = "session-memory-async"
    user_id = "3"
    memory_manager.clear_memory(session_id, user_id)
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                agent_messages=[
                    {
                        "type": "human",
                        "data": {
                            "content": "异步恢复",
                            "additional_kwargs": {},
                            "response_metadata": {},
                            "type": "human",
                            "name": None,
                            "id": None,
                            "example": False,
                        },
                    }
                ]
            )
        )
    )
    monkeypatch.setattr(
        "app.agent.memory.get_configured_agent_chat_service",
        lambda: service,
    )

    messages = asyncio.run(
        memory_manager.async_get_agent_messages(
            session_id=session_id,
            user_id=user_id,
        )
    )

    assert len(messages) == 1
    assert messages[0].content == "异步恢复"
    service.get.assert_awaited_once_with(
        session_id=session_id,
        user_id=user_id,
    )
