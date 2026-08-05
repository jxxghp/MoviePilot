import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from app.agent import MoviePilotAgent
from app.agent.memory import memory_manager
from app.core.config import settings
from app.schemas.types import ChainEventType, EventType


class _FakeGraphState:
    """提供 LangGraph get_state 测试替身。"""

    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeAgent:
    """提供非流式 Agent 执行测试替身。"""

    def __init__(self, messages):
        self._messages = messages

    async def ainvoke(self, _payload, config=None):
        """模拟成功完成 Agent 调用。"""
        return None

    def get_state(self, _config):
        """返回测试消息状态。"""
        return _FakeGraphState(self._messages)


class _FakeFailingAgent(_FakeAgent):
    """提供失败 Agent 执行测试替身。"""

    async def ainvoke(self, _payload, config=None):
        """模拟 Agent 调用失败。"""
        raise RuntimeError("llm failed")


def test_initialize_llm_uses_chain_event_selection(monkeypatch) -> None:
    """Agent 初始化 LLM 时应优先使用链式事件返回的供应商配置。"""
    monkeypatch.setattr(settings, "LLM_THINKING_LEVEL", "xhigh")
    agent = MoviePilotAgent(session_id="agent-tokens-test", user_id="user-1")
    fake_llm = object()

    async def select_provider(event_type, event_data):
        """模拟 Agent Tokens 插件写入供应商配置。"""
        assert event_type == ChainEventType.AgentLLMProvider
        event_data.provider = "openai"
        event_data.base_url = "https://tokens.example.com/v1"
        event_data.api_key = "sk-agent-token"
        event_data.model = "free-model"
        event_data.base_url_preset = None
        event_data.user_agent = "AgentTokens-UA/1.0"
        event_data.selected_provider_id = "provider-1"
        event_data.selected_provider_name = "Free Provider"
        event_data.source = "AgentTokens"
        return SimpleNamespace(event_data=event_data)

    with (
        patch(
            "app.agent.eventmanager.async_send_event",
            new=AsyncMock(side_effect=select_provider),
        ) as send_event,
        patch(
            "app.agent.LLMHelper.get_llm",
            new=AsyncMock(return_value=fake_llm),
        ) as get_llm,
    ):
        result = asyncio.run(agent._initialize_llm(streaming=True))
        second_result = asyncio.run(agent._initialize_llm(streaming=False))

    assert result is fake_llm
    assert second_result is fake_llm
    send_event.assert_awaited_once()
    assert get_llm.await_count == 2
    get_llm.assert_any_await(
        streaming=True,
        provider="openai",
        model="free-model",
        api_key="sk-agent-token",
        base_url="https://tokens.example.com/v1",
        base_url_preset=None,
        user_agent="AgentTokens-UA/1.0",
        use_proxy=True,
        thinking_level="xhigh",
        api_protocol="auto",
        web_search_mode="local",
    )
    assert agent._llm_provider_selection["selected_provider_id"] == "provider-1"


def test_execute_agent_broadcasts_usage_on_success() -> None:
    """Agent 执行成功后应广播聚合 token 用量事件。"""
    agent = MoviePilotAgent(session_id="usage-success", user_id="user-1")
    agent._should_stream = lambda: False
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )
    agent.send_agent_message = AsyncMock()

    async def create_agent(_streaming=False, streaming=False):
        """模拟创建 Agent 时完成供应商选择和用量统计。"""
        agent._llm_provider_selection = {
            "selected_provider_id": "provider-1",
            "selected_provider_name": "Free Provider",
            "provider": "openai",
            "base_url": "https://tokens.example.com/v1",
            "model": "free-model",
            "source": "AgentTokens",
        }
        agent._record_usage(
            {
                "has_usage": True,
                "model": "free-model",
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            }
        )
        return _FakeAgent([AIMessage(content="ok")])

    with (
        patch.object(agent, "_create_agent", new=create_agent),
        patch.object(memory_manager, "save_agent_messages"),
        patch("app.agent.eventmanager.send_event") as send_event,
    ):
        asyncio.run(agent._execute_agent([]))

    send_event.assert_called_once()
    assert send_event.call_args.args[0] == EventType.AgentTokensUsage
    usage = send_event.call_args.args[1]
    assert usage.success
    assert usage.selected_provider_id == "provider-1"
    assert usage.input_tokens == 12
    assert usage.output_tokens == 8
    assert usage.total_tokens == 20


def test_execute_agent_broadcasts_usage_on_failure() -> None:
    """Agent 执行失败后仍应广播用量事件。"""
    agent = MoviePilotAgent(session_id="usage-failure", user_id="user-1")
    agent._should_stream = lambda: False
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )
    agent.send_agent_message = AsyncMock()

    async def create_agent(_streaming=False, streaming=False):
        """模拟创建 Agent 时已选中供应商但执行失败。"""
        agent._llm_provider_selection = {
            "selected_provider_id": "provider-2",
            "selected_provider_name": "Backup Provider",
            "provider": "openai",
            "base_url": "https://backup.example.com/v1",
            "model": "backup-model",
            "source": "AgentTokens",
        }
        return _FakeFailingAgent([])

    with (
        patch.object(agent, "_create_agent", new=create_agent),
        patch("app.agent.eventmanager.send_event") as send_event,
    ):
        result, _ = asyncio.run(agent._execute_agent([]))

    assert "智能助手执行失败" in result
    send_event.assert_called_once()
    assert send_event.call_args.args[0] == EventType.AgentTokensUsage
    usage = send_event.call_args.args[1]
    assert not usage.success
    assert usage.selected_provider_id == "provider-2"
    assert "llm failed" in usage.error
