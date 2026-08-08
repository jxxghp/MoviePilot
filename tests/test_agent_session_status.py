import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from app.agent.middleware.usage import UsageMiddleware
from app.agent import AgentManager
from app.chain.message import MessageChain
from app.schemas.types import MessageChannel


class TestAgentSessionStatus(unittest.TestCase):
    def setUp(self):
        """清理跨用例共享的用户会话状态。"""
        MessageChain._user_sessions.clear()

    def tearDown(self):
        """清理测试产生的用户会话状态。"""
        MessageChain._user_sessions.clear()

    def test_usage_middleware_records_usage_metadata(self):
        snapshots = []
        middleware = UsageMiddleware(on_usage=snapshots.append)
        request = ModelRequest(
            model=SimpleNamespace(
                model="gpt-4o-mini", profile={"max_input_tokens": 128000}
            ),
            messages=[],
            state={},
            runtime=None,
        )
        response = ModelResponse(
            result=[
                AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 1200,
                        "output_tokens": 300,
                        "total_tokens": 1500,
                    },
                )
            ]
        )

        async def handler(_: ModelRequest):
            return response

        result = asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertIs(result, response)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["model"], "gpt-4o-mini")
        self.assertEqual(snapshots[0]["context_window_tokens"], 128000)
        self.assertEqual(snapshots[0]["input_tokens"], 1200)
        self.assertEqual(snapshots[0]["output_tokens"], 300)
        self.assertEqual(snapshots[0]["total_tokens"], 1500)
        self.assertAlmostEqual(snapshots[0]["context_usage_ratio"], 1200 / 128000)

    def test_remote_session_status_sends_usage_summary(self):
        chain = MessageChain()
        chain._user_sessions["10001"] = ("session-1", datetime.now())
        status = {
            "session_id": "session-1",
            "model": "gpt-4o-mini",
            "context_window_tokens": 128000,
            "last_input_usage_available": True,
            "last_input_tokens": 1200,
            "last_output_tokens": 300,
            "last_total_tokens": 1500,
            "last_context_usage_ratio": 1200 / 128000,
            "last_request_estimate_available": True,
            "last_estimated_input_tokens": 1300,
            "last_estimated_message_tokens": 900,
            "last_estimated_system_tokens": 250,
            "last_estimated_tool_tokens": 150,
            "last_estimated_multimodal_tokens": 0,
            "last_estimated_input_ratio": 1300 / 128000,
            "last_estimated_over_input_limit": False,
            "last_actual_input_tokens": 1200,
            "last_estimate_error_tokens": -100,
            "last_estimate_error_ratio": -100 / 1200,
            "total_input_tokens": 4500,
            "total_output_tokens": 1500,
            "total_tokens": 6000,
            "model_call_count": 4,
            "last_updated_at": "2026-04-26 12:34:56",
            "is_processing": True,
            "pending_messages": 2,
        }

        with (
            patch(
                "app.agent.agent_manager.get_session_status",
                return_value=status,
            ),
            patch.object(chain, "post_message") as post_message,
        ):
            chain.remote_session_status(
                channel=MessageChannel.Telegram,
                userid="10001",
                source="telegram-test",
            )

        notification = post_message.call_args.args[0]
        self.assertEqual(notification.title, "当前智能体会话状态")
        self.assertIn("session-1", notification.text)
        self.assertIn("gpt-4o-mini", notification.text)
        self.assertIn("1,200 / 128,000 (0.94%)", notification.text)
        self.assertIn("最终请求估算: 1,300 / 128,000 (1.02%)", notification.text)
        self.assertIn("消息 900 / 系统 250 / 工具 150 / 其中图片固定成本 0", notification.text)
        self.assertIn("实际 1,200 / 误差 -100 (-8.33%)", notification.text)
        self.assertIn("输入 4,500 / 输出 1,500 / 总计 6,000", notification.text)
        self.assertIn("运行中", notification.text)

    def test_remote_session_status_handles_missing_session(self):
        chain = MessageChain()

        with patch.object(chain, "post_message") as post_message:
            chain.remote_session_status(
                channel=MessageChannel.Telegram,
                userid="10001",
                source="telegram-test",
            )

        notification = post_message.call_args.args[0]
        self.assertEqual(notification.title, "您当前没有活跃的智能体会话")

    def test_get_or_create_session_cleans_expired_session(self):
        """用户会话超过复用窗口时应调度清理旧 Agent 会话。"""
        chain = MessageChain()
        chain._user_sessions.clear()
        chain._user_sessions["10001"] = (
            "old-session",
            datetime.now() - timedelta(minutes=chain._session_timeout_minutes + 1),
        )

        with patch.object(chain, "_schedule_agent_session_clear") as clear_session:
            session_id = chain._get_or_create_session_id("10001")

        self.assertNotEqual(session_id, "old-session")
        self.assertEqual(chain._user_sessions["10001"][0], session_id)
        clear_session.assert_called_once_with("old-session", "10001")

    def test_agent_manager_collects_idle_sessions(self):
        """Agent 管理器应只回收超过空闲窗口且未忙碌的会话。"""
        manager = AgentManager()
        manager._idle_session_ttl = timedelta(seconds=1)
        manager._session_last_used["idle-session"] = (
            "10001",
            datetime.now() - timedelta(seconds=2),
        )
        manager._session_last_used["fresh-session"] = ("10002", datetime.now())

        self.assertEqual(
            [("idle-session", "10001")],
            manager._expired_idle_sessions(),
        )
