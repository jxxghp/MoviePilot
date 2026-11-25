"""
验证会话用量汇总、过期清理及运行状态展示。
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from app.agent.middleware.usage import UsageMiddleware
from app.agent.manager import AgentManager
from app.chain.message import MessageChain
from app.schemas.types import NotificationChannel


@pytest.fixture(autouse=True)
def _isolate_test_state():
    """
    隔离测试之间共享的会话与交互状态，并在结束时清理。
    """
    MessageChain._user_sessions.clear()
    try:
        yield
    finally:
        MessageChain._user_sessions.clear()


def test_usage_middleware_records_usage_metadata() -> None:
    """
    用量中间件记录模型、上下文容量与输入输出消耗。
    """
    snapshots = []
    middleware = UsageMiddleware(on_usage=snapshots.append)
    request = ModelRequest(
        model=SimpleNamespace(model="gpt-4o-mini", profile={"max_input_tokens": 128000}),
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

    assert result is response
    assert len(snapshots) == 1
    assert snapshots[0]["model"] == "gpt-4o-mini"
    assert snapshots[0]["context_window_tokens"] == 128000
    assert snapshots[0]["input_tokens"] == 1200
    assert snapshots[0]["output_tokens"] == 300
    assert snapshots[0]["total_tokens"] == 1500
    assert round(abs(snapshots[0]["context_usage_ratio"] - 1200 / 128000), 7) == 0


def test_remote_session_status_sends_usage_summary() -> None:
    """
    远程会话状态回复包含用量、估算偏差和运行状态。
    """
    chain = MessageChain()
    chain._user_sessions[chain._session_user_key("10001", NotificationChannel.Telegram, "telegram-test")] = (
        "session-1", datetime.now(),
    )
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
            "app.chain.message.get_running_agent_manager",
            return_value=SimpleNamespace(get_session_status=lambda **_: status),
        ),
        patch.object(chain, "post_message") as post_message,
    ):
        chain.remote_session_status(
            channel=NotificationChannel.Telegram,
            userid="10001",
            source="telegram-test",
        )

    notification = post_message.call_args.args[0]
    assert notification.title == "当前智能体会话状态"
    assert "session-1" in notification.text
    assert "gpt-4o-mini" in notification.text
    assert "1,200 / 128,000 (0.94%)" in notification.text
    assert "最终请求估算: 1,300 / 128,000 (1.02%)" in notification.text
    assert "消息 900 / 系统 250 / 工具 150 / 其中图片固定成本 0" in notification.text
    assert "实际 1,200 / 误差 -100 (-8.33%)" in notification.text
    assert "输入 4,500 / 输出 1,500 / 总计 6,000" in notification.text
    assert "运行中" in notification.text


def test_remote_session_status_handles_missing_session() -> None:
    """
    没有活跃会话时返回明确提示。
    """
    chain = MessageChain()

    with patch.object(chain, "post_message") as post_message:
        chain.remote_session_status(
            channel=NotificationChannel.Telegram,
            userid="10001",
            source="telegram-test",
        )

    notification = post_message.call_args.args[0]
    assert notification.title == "您当前没有活跃的智能体会话"


def test_get_or_create_session_cleans_expired_session() -> None:
    """
    用户会话超过复用窗口时应调度清理旧 Agent 会话。
    """
    chain = MessageChain()
    chain._user_sessions.clear()
    chain._user_sessions["10001"] = (
        "old-session",
        datetime.now() - timedelta(minutes=chain._session_timeout_minutes + 1),
    )

    with patch.object(chain, "_schedule_agent_session_clear") as clear_session:
        session_id = chain._get_or_create_session_id("10001")

    assert session_id != "old-session"
    assert chain._user_sessions["10001"][0] == session_id
    clear_session.assert_called_once_with("old-session", "10001")


def test_agent_manager_collects_idle_sessions() -> None:
    """
    Agent 管理器应只回收超过空闲窗口且未忙碌的会话。
    """
    manager = AgentManager()
    manager._idle_session_ttl = timedelta(seconds=1)
    manager._session_last_used["idle-session"] = (
        "10001",
        datetime.now() - timedelta(seconds=2),
    )
    manager._session_last_used["fresh-session"] = ("10002", datetime.now())

    assert [("idle-session", "10001")] == manager._expired_idle_sessions()
