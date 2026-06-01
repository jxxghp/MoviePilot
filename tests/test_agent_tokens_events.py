import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.agent import MoviePilotAgent
from app.agent.memory import memory_manager
# agenttokens 为动态安装插件（app/plugins/** 被 gitignore，CI / 全新环境无此插件），
# 缺失时跳过本模块，避免 collection 阶段 ImportError。
AgentTokens = pytest.importorskip("app.plugins.agenttokens").AgentTokens
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


class AgentTokensEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_sidebar_nav_respects_config(self):
        """插件侧边栏入口应受 show_sidebar_nav 配置控制。"""
        plugin = AgentTokens()

        with patch.object(plugin, "update_config"):
            plugin.init_plugin(
                {
                    "enabled": True,
                    "show_sidebar_nav": False,
                    "providers": [],
                }
            )
            self.assertEqual([], plugin.get_sidebar_nav())

            plugin.init_plugin(
                {
                    "enabled": True,
                    "show_sidebar_nav": True,
                    "providers": [],
                }
            )
            nav = plugin.get_sidebar_nav()

        self.assertEqual("Agent Tokens 管理", nav[0]["title"])

    async def test_initialize_llm_uses_chain_event_selection(self):
        """Agent 初始化 LLM 时应优先使用链式事件返回的供应商配置。"""
        agent = MoviePilotAgent(session_id="agent-tokens-test", user_id="user-1")
        fake_llm = object()

        async def select_provider(etype, data):
            """模拟 Agent Tokens 插件写入供应商配置。"""
            self.assertEqual(ChainEventType.AgentLLMProvider, etype)
            data.provider = "openai"
            data.base_url = "https://tokens.example.com/v1"
            data.api_key = "sk-agent-token"
            data.model = "free-model"
            data.base_url_preset = None
            data.user_agent = "AgentTokens-UA/1.0"
            data.selected_provider_id = "provider-1"
            data.selected_provider_name = "Free Provider"
            data.source = "AgentTokens"
            return SimpleNamespace(event_data=data)

        with (
            patch(
                "app.agent.eventmanager.async_send_event",
                new=AsyncMock(side_effect=select_provider),
            ) as send_event,
            patch("app.agent.LLMHelper.get_llm", new=AsyncMock(return_value=fake_llm)) as get_llm,
        ):
            result = await agent._initialize_llm(streaming=True)
            second_result = await agent._initialize_llm(streaming=False)

        self.assertIs(result, fake_llm)
        self.assertIs(second_result, fake_llm)
        send_event.assert_awaited_once()
        self.assertEqual(2, get_llm.await_count)
        get_llm.assert_any_await(
            streaming=True,
            provider="openai",
            model="free-model",
            api_key="sk-agent-token",
            base_url="https://tokens.example.com/v1",
            base_url_preset=None,
            user_agent="AgentTokens-UA/1.0",
            use_proxy=True,
            thinking_level=None,
        )
        self.assertEqual("provider-1", agent._llm_provider_selection["selected_provider_id"])

    async def test_execute_agent_broadcasts_usage_on_success(self):
        """Agent 执行成功后应广播聚合 token 用量事件。"""
        agent = MoviePilotAgent(session_id="usage-success", user_id="user-1")
        agent._should_stream = lambda: False
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent.send_agent_message = AsyncMock()
        agent._save_agent_message_to_db = AsyncMock()

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
            await agent._execute_agent([])

        send_event.assert_called_once()
        self.assertEqual(EventType.AgentTokensUsage, send_event.call_args.args[0])
        usage = send_event.call_args.args[1]
        self.assertTrue(usage.success)
        self.assertEqual("provider-1", usage.selected_provider_id)
        self.assertEqual(12, usage.input_tokens)
        self.assertEqual(8, usage.output_tokens)
        self.assertEqual(20, usage.total_tokens)

    async def test_execute_agent_broadcasts_usage_on_failure(self):
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
            result, _ = await agent._execute_agent([])

        self.assertIn("智能助手执行失败", result)
        send_event.assert_called_once()
        self.assertEqual(EventType.AgentTokensUsage, send_event.call_args.args[0])
        usage = send_event.call_args.args[1]
        self.assertFalse(usage.success)
        self.assertEqual("provider-2", usage.selected_provider_id)
        self.assertIn("llm failed", usage.error)
