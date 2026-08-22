"""Agent Telegram Rich Message 契约测试。"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.agent.orchestrator import MoviePilotAgent
from app.agent.prompt import prompt_manager
from app.agent.tools.impl.send_message import SendMessageInput, SendMessageTool
from app.chain.agent import AgentChain
from app.schemas.types import NotificationChannel


def test_send_message_input_accepts_rich_message_only() -> None:
    """Rich Message 本身应能构成完整的工具载荷。"""
    payload = SendMessageInput(rich_message="# 结果\n\n- 成功")

    assert payload.message is None
    assert payload.rich_message == "# 结果\n\n- 成功"


def test_send_message_tool_keeps_plain_fallback_for_rich_message() -> None:
    """工具应同时保留跨渠道文本回退和 Telegram Rich Markdown。"""

    async def _run():
        tool = SendMessageTool(session_id="session-1", user_id="10001")
        tool.set_message_attr(
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        tool.set_agent_context(agent_context={})
        with patch(
            "app.agent.tools.base.ToolChain.async_post_message",
            new_callable=AsyncMock,
        ) as async_post_message:
            result = await tool.run(rich_message="# 结果\n\n- **成功**")
        return result, async_post_message

    result, async_post_message = asyncio.run(_run())
    message = async_post_message.await_args.args[0]

    assert result == "消息已发送"
    assert message.text == "# 结果\n\n- **成功**"
    assert message.rich_message == "# 结果\n\n- **成功**"
    assert message.save_history is False


def test_agent_prompt_prefers_rich_message_only_for_telegram() -> None:
    """仅 Telegram 会话应收到 Rich Message 优先提示。"""
    telegram_prompt = prompt_manager.get_agent_prompt(
        channel=NotificationChannel.Telegram.value
    )
    wechat_prompt = prompt_manager.get_agent_prompt(
        channel=NotificationChannel.Wechat.value
    )

    assert "`rich_message` argument" in telegram_prompt
    assert "GitHub-style Markdown" in telegram_prompt
    assert "`rich_message` argument" not in wechat_prompt


def test_agent_direct_telegram_reply_uses_rich_message() -> None:
    """Agent 常规 Telegram 回复也应自动携带 Rich Markdown。"""

    async def _run():
        agent = MoviePilotAgent(session_id="telegram-session", user_id="10001")
        agent.channel = NotificationChannel.Telegram.value
        agent.source = "telegram-test"
        agent.username = "tester"
        with patch.object(
            AgentChain,
            "async_post_message",
            new_callable=AsyncMock,
        ) as async_post_message:
            await agent.send_agent_message("# 结果\n\n- **完成**")
        return async_post_message

    async_post_message = asyncio.run(_run())
    message = async_post_message.await_args.args[0]

    assert message.text == "# 结果\n\n- **完成**"
    assert message.rich_message == "# 结果\n\n- **完成**"
