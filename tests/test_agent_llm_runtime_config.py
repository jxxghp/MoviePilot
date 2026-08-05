import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent import MoviePilotAgent
from app.core.config import settings
from app.schemas import AgentLLMProviderEventData
from app.schemas.types import ChainEventType


def test_resolve_llm_runtime_config_uses_system_thinking_level(monkeypatch) -> None:
    """插件未提供有效思考强度时应使用系统配置。"""
    monkeypatch.setattr(settings, "LLM_THINKING_LEVEL", "xhigh")
    agent = MoviePilotAgent(session_id="thinking-level-default", user_id="user-1")

    async def return_empty_config(event_type, event_data):
        """模拟插件未返回有效运行时配置。"""
        assert event_type == ChainEventType.AgentLLMProvider
        assert event_data.thinking_level == "xhigh"
        return SimpleNamespace(event_data=AgentLLMProviderEventData())

    with patch(
        "app.agent.eventmanager.async_send_event",
        new=AsyncMock(side_effect=return_empty_config),
    ):
        runtime_config = asyncio.run(agent._resolve_llm_runtime_config())

    assert runtime_config["thinking_level"] == "xhigh"


def test_resolve_llm_runtime_config_prefers_plugin_thinking_level(monkeypatch) -> None:
    """插件显式覆盖思考强度时应优先使用插件值。"""
    monkeypatch.setattr(settings, "LLM_THINKING_LEVEL", "xhigh")
    agent = MoviePilotAgent(session_id="thinking-level-plugin", user_id="user-1")

    async def override_thinking_level(_event_type, event_data):
        """模拟插件覆盖思考强度。"""
        event_data.thinking_level = "high"
        return SimpleNamespace(event_data=event_data)

    with patch(
        "app.agent.eventmanager.async_send_event",
        new=AsyncMock(side_effect=override_thinking_level),
    ):
        runtime_config = asyncio.run(agent._resolve_llm_runtime_config())

    assert runtime_config["thinking_level"] == "high"


def test_resolve_llm_runtime_config_uses_system_api_protocol(monkeypatch) -> None:
    """插件未提供 API 协议时应使用系统配置。"""
    monkeypatch.setattr(settings, "LLM_API_PROTOCOL", "responses")
    agent = MoviePilotAgent(session_id="api-protocol-default", user_id="user-1")

    async def return_empty_config(event_type, event_data):
        """模拟插件未返回有效运行时配置。"""
        assert event_type == ChainEventType.AgentLLMProvider
        assert event_data.api_protocol == "responses"
        return SimpleNamespace(event_data=AgentLLMProviderEventData())

    with patch(
        "app.agent.eventmanager.async_send_event",
        new=AsyncMock(side_effect=return_empty_config),
    ):
        runtime_config = asyncio.run(agent._resolve_llm_runtime_config())

    assert runtime_config["api_protocol"] == "responses"


def test_resolve_llm_runtime_config_prefers_plugin_api_protocol(monkeypatch) -> None:
    """插件显式覆盖 API 协议时应优先使用插件值。"""
    monkeypatch.setattr(settings, "LLM_API_PROTOCOL", "responses")
    agent = MoviePilotAgent(session_id="api-protocol-plugin", user_id="user-1")

    async def override_api_protocol(_event_type, event_data):
        """模拟插件覆盖 API 协议。"""
        event_data.api_protocol = "chat_completions"
        return SimpleNamespace(event_data=event_data)

    with patch(
        "app.agent.eventmanager.async_send_event",
        new=AsyncMock(side_effect=override_api_protocol),
    ):
        runtime_config = asyncio.run(agent._resolve_llm_runtime_config())

    assert runtime_config["api_protocol"] == "chat_completions"


def test_resolve_llm_runtime_config_prefers_plugin_web_search_mode(monkeypatch) -> None:
    """插件显式覆盖联网搜索模式时应优先使用插件值。"""
    monkeypatch.setattr(settings, "LLM_WEB_SEARCH_MODE", "local")
    agent = MoviePilotAgent(session_id="web-search-plugin", user_id="user-1")

    async def override_web_search_mode(_event_type, event_data):
        """模拟插件覆盖联网搜索模式。"""
        event_data.web_search_mode = "builtin"
        return SimpleNamespace(event_data=event_data)

    with patch(
        "app.agent.eventmanager.async_send_event",
        new=AsyncMock(side_effect=override_web_search_mode),
    ):
        runtime_config = asyncio.run(agent._resolve_llm_runtime_config())

    assert runtime_config["web_search_mode"] == "builtin"
