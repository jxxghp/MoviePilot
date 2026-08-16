"""Agent 工具回调消息回复目标（original_chat_id 回填）的测试。"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.ask_user_choice import (
    AskUserChoiceTool,
    UserChoiceOptionInput,
)
from app.agent.tools.impl.send_message import SendMessageTool
from app.schemas import Message
from app.schemas.types import NotificationChannel


def _run_choice_tool(agent_context: dict, channel: str, source: str) -> Message:
    """运行按钮选择工具并返回其发送的通知。"""
    tool = AskUserChoiceTool(session_id="session-1", user_id="ou_xxx")
    tool.set_message_attr(
        channel=channel,
        source=source,
        username="tester",
    )
    tool.set_agent_context(agent_context=agent_context)

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new=AsyncMock(),
    ) as async_post_message:
        asyncio.run(
            tool.run(
                message="请选择",
                options=[UserChoiceOptionInput(label="继续", value="继续")],
            )
        )

    assert async_post_message.await_count == 1
    return async_post_message.await_args.args[0]


def test_choice_tool_backfills_original_chat_id_from_session_context():
    """群聊场景下按钮选择通知应回填会话上下文中的 original_chat_id。"""
    notification = _run_choice_tool(
        agent_context={"original_chat_id": "oc_group_123"},
        channel=NotificationChannel.Feishu.value,
        source="feishu-test",
    )

    assert notification.original_chat_id == "oc_group_123"
    assert notification.userid == "ou_xxx"


def test_choice_tool_keeps_explicit_original_chat_id():
    """按钮选择通知已显式携带原会话 ID 时不应被上下文覆盖。"""
    notification = _run_choice_tool(
        agent_context={"original_chat_id": "oc_group_zzz"},
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
    )

    assert notification.original_chat_id == "oc_group_zzz"


def test_choice_tool_no_context_does_not_backfill():
    """会话上下文未携带原会话 ID 时，通知保持原有发送目标。"""
    notification = _run_choice_tool(
        agent_context={},
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
    )

    assert notification.original_chat_id is None


def test_background_tool_clears_original_chat_id():
    """无渠道上下文的后台任务应清空渠道定位信息交由消息链广播。"""
    tool = SendMessageTool(session_id="session-1", user_id="ou_xxx")
    tool.set_agent_context(agent_context={"original_chat_id": "oc_group_123"})

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new=AsyncMock(),
    ) as async_post_message:
        asyncio.run(tool.send_tool_message("后台任务执行完成"))

    notification = async_post_message.await_args.args[0]
    assert notification.original_chat_id is None
    assert notification.channel is None
    assert notification.userid is None


def test_send_tool_message_backfills_original_chat_id():
    """send_tool_message 工具消息同样应回填原会话 ID。"""
    tool = SendMessageTool(session_id="session-1", user_id="ou_xxx")
    tool.set_message_attr(
        channel=NotificationChannel.Feishu.value,
        source="feishu-test",
        username="tester",
    )
    tool.set_agent_context(agent_context={"original_chat_id": "oc_group_123"})

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new=AsyncMock(),
    ) as async_post_message:
        asyncio.run(tool.send_tool_message("正在执行操作"))

    notification = async_post_message.await_args.args[0]
    assert notification.original_chat_id == "oc_group_123"


def test_tool_context_includes_original_chat_id():
    """工具共享上下文应携带当前会话的原会话 ID。"""
    from app.agent import MoviePilotAgent

    agent = MoviePilotAgent(
        session_id="session-1",
        user_id="ou_xxx",
        channel=NotificationChannel.Feishu.value,
        source="feishu-test",
        username="tester",
        original_chat_id="oc_group_123",
    )

    with patch.object(MoviePilotAgent, "_is_system_admin_context", return_value=False):
        context = asyncio.run(agent._build_tool_context(should_dispatch_reply=True))

    assert context["original_chat_id"] == "oc_group_123"