"""Agent 图缓存行为测试。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import MoviePilotAgent, ReplyMode, _CompiledAgentBundle
from app.core.config import settings


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。"""
    return "asyncio"


class _FakeGraphState:
    """提供 LangGraph get_state 测试替身。"""

    def __init__(self, messages):
        """保存测试消息状态。"""
        self.values = {"messages": messages}


class _CapturingAgent:
    """捕获传入消息的非流式 Agent 测试替身。"""

    def __init__(self):
        """初始化捕获容器。"""
        self.payload = None

    async def ainvoke(self, payload, config=None):
        """记录 Agent 调用输入。"""
        self.payload = payload

    def get_state(self, _config):
        """返回包含最终 AI 回复的图状态。"""
        return _FakeGraphState([AIMessage(content="ok")])


@pytest.mark.anyio
async def test_create_agent_reuses_cached_graph_when_signature_matches():
    """构造签名一致时应直接复用已编译 Agent 图。"""
    cached_graph = object()
    agent = MoviePilotAgent(session_id="cache-hit", user_id="user-1")
    agent._compiled_agent_bundle = _CompiledAgentBundle(
        signature=("sig",),
        agent=cached_graph,
        streaming=False,
        created_at=datetime.now(),
    )

    with patch.object(
        agent,
        "_agent_bundle_signature",
        new=AsyncMock(return_value=("sig",)),
    ), patch("app.agent.create_agent") as create_agent:
        graph = await agent._create_agent(streaming=False)

    assert graph is cached_graph
    assert agent._last_agent_cache_hit is True
    create_agent.assert_not_called()


@pytest.mark.anyio
async def test_agent_bundle_signature_changes_with_temperature(monkeypatch) -> None:
    """温度配置变化时应使会话内 Agent 图缓存失效。"""
    agent = MoviePilotAgent(session_id="temperature-change", user_id="user-1")
    runtime_config = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "test-key",
        "base_url": "https://llm.example.com/v1",
        "base_url_preset": None,
        "user_agent": None,
        "use_proxy": False,
        "thinking_level": "off",
    }

    with patch.object(
        agent,
        "_resolve_llm_runtime_config",
        new=AsyncMock(return_value=runtime_config),
    ):
        monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.3)
        initial_signature = await agent._agent_bundle_signature(streaming=False)
        monkeypatch.setattr(settings, "LLM_TEMPERATURE", 1.0)
        updated_signature = await agent._agent_bundle_signature(streaming=False)

    assert updated_signature != initial_signature


@pytest.mark.anyio
async def test_execute_agent_sends_only_latest_message_on_cache_hit():
    """缓存命中时只把本轮新消息交给 LangGraph，避免重复提交历史。"""
    fake_graph = _CapturingAgent()
    agent = MoviePilotAgent(session_id="cache-hit", user_id="user-1")
    agent.reply_mode = ReplyMode.CAPTURE_ONLY
    agent._tool_context = {"user_reply_sent": False}
    agent._streamed_output = ""
    agent._should_stream = lambda: False
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )

    async def _create_agent(streaming=False):
        """模拟缓存命中后的 Agent 创建结果。"""
        agent._last_agent_cache_hit = True
        return fake_graph

    agent._create_agent = _create_agent
    messages = [HumanMessage(content="上一轮"), HumanMessage(content="本轮")]

    with patch("app.agent.eventmanager.send_event"):
        await agent._execute_agent(messages)

    assert agent._streamed_output == "ok"
    assert fake_graph.payload["messages"] == [messages[-1]]
