import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.memory import MemoryManager, memory_manager
from app.agent.orchestrator import HEARTBEAT_SESSION_PREFIX, MoviePilotAgent
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
    isolated_memory = MemoryManager()
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

    messages = isolated_memory.get_agent_messages(
        session_id=session_id,
        user_id=user_id,
    )

    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].content == "继续之前的话题"


def test_async_memory_manager_restores_through_native_async_service(monkeypatch):
    """异步记忆恢复只能通过会话应用服务的异步查询端口。"""
    session_id = "session-memory-async"
    user_id = "3"
    isolated_memory = MemoryManager()
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
        isolated_memory.async_get_agent_messages(
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


def test_memory_restore_entries_share_lookup_and_projection(monkeypatch):
    """记忆同步、异步恢复只保留查询 I/O 差异并共享回退与投影决策。"""
    snapshot = SimpleNamespace(agent_messages=[
        {
            "type": "human",
            "data": {
                "content": "共享恢复",
                "additional_kwargs": {},
                "response_metadata": {},
                "type": "human",
                "name": None,
                "id": None,
                "example": False,
            },
        }
    ])
    chat = MagicMock()
    chat.get_sync.side_effect = [None, snapshot]
    chat.get = AsyncMock(side_effect=[None, snapshot])
    manager = MemoryManager(chat=chat, persistence=MagicMock())
    lookup = MagicMock(wraps=manager._chat_lookup_params)
    restore = MagicMock(wraps=manager._restore_agent_messages)
    monkeypatch.setattr(manager, "_chat_lookup_params", lookup)
    monkeypatch.setattr(manager, "_restore_agent_messages", restore)

    sync_messages = manager.get_agent_messages("session-parity", "user-parity")
    manager.clear_memory("session-parity", "user-parity")
    async_messages = asyncio.run(
        manager.async_get_agent_messages("session-parity", "user-parity")
    )

    assert [message.content for message in sync_messages] == ["共享恢复"]
    assert [message.content for message in async_messages] == ["共享恢复"]
    assert lookup.call_args_list == [
        call("session-parity", "user-parity"),
        call("session-parity", "user-parity"),
    ]
    assert restore.call_count == 2
    expected_calls = [
        call(session_id="session-parity", user_id="user-parity", chat=snapshot),
        call(session_id="session-parity", user_id="user-parity", chat=snapshot),
    ]
    assert restore.call_args_list == expected_calls
    assert chat.get_sync.call_args_list == [
        call(session_id="session-parity", user_id="user-parity"),
        call(session_id="session-parity"),
    ]
    assert chat.get.await_args_list == [
        call(session_id="session-parity", user_id="user-parity"),
        call(session_id="session-parity"),
    ]


def test_memory_save_entries_share_cache_state_projection(monkeypatch):
    """记忆同步、异步保存必须共享内存状态更新和消息序列化结果。"""
    chat = MagicMock()
    persistence = MagicMock()
    persistence.async_save_agent_messages = AsyncMock()
    manager = MemoryManager(chat=chat, persistence=persistence)
    update = MagicMock(wraps=manager._update_agent_messages)
    monkeypatch.setattr(manager, "_update_agent_messages", update)
    messages = [HumanMessage(content="共享保存")]

    manager.save_agent_messages("session-save", "user-save", messages)
    asyncio.run(
        manager.async_save_agent_messages("session-save", "user-save", messages)
    )

    expected = {
        "session_id": "session-save",
        "user_id": "user-save",
        "messages": messages,
    }
    assert update.call_args_list == [call(**expected), call(**expected)]
    assert chat.save_agent_messages.call_args.kwargs == (
        persistence.async_save_agent_messages.await_args.kwargs
    )
