"""
Agent Telegram Rich Message 契约测试。
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy.contracts import ExecutionOutcome
from app.agent.prompt import prompt_manager
from app.agent.tools.impl.send_message import SendMessageInput, SendMessageTool
from app.agent.tools.result import inspect_tool_result
from app.chain.agent import AgentChain
from app.schemas.message import Message
from app.schemas.types import MessageType, NotificationChannel


def test_send_message_input_accepts_rich_message_only() -> None:
    """
    Rich Message 本身应能构成完整的工具载荷。
    """
    payload = SendMessageInput(rich_message="# 结果\n\n- 成功")

    assert payload.message is None
    assert payload.rich_message == "# 结果\n\n- 成功"


def test_send_message_tool_keeps_plain_fallback_for_rich_message() -> None:
    """
    工具应同时保留跨渠道文本回退和 Telegram Rich Markdown。
    """

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
    """
    仅 Telegram 会话应收到 Rich Message 优先提示。
    """
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
    """
    Agent 常规 Telegram 回复也应自动携带 Rich Markdown。
    """

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


def test_send_message_tool_reports_failed_when_channel_delivery_unconfirmed() -> None:
    """
    交互渠道发送未确认时，send_message 必须返回失败状态且不得标记已回复。
    """

    async def _run():
        tool = SendMessageTool(session_id="session-1", user_id="10001")
        tool.set_message_attr(
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        agent_context: dict[str, object] = {}
        tool.set_agent_context(agent_context=agent_context)
        with patch(
            "app.agent.tools.base.ToolChain.async_post_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Telegram 通知发送未确认：Telegram"),
        ) as async_post_message:
            result = await tool.run(rich_message="# 最终回复")
        return result, agent_context, async_post_message

    result, agent_context, async_post_message = asyncio.run(_run())
    payload = json.loads(result)

    assert async_post_message.await_args.kwargs == {"_strict_delivery": True}
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED
    assert payload["success"] is False
    assert payload["execution_outcome"] == "failed"
    assert "Telegram 通知发送未确认" in payload["error"]
    assert agent_context.get("user_reply_sent") is None


def test_async_post_message_strict_delivery_propagates_module_error() -> None:
    """
    异步通知在严格投递模式下应透传模块发送未确认异常，普通模式仍隔离错误。
    """
    chain = AgentChain()
    message = Message(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        mtype=MessageType.Other,
        userid="10001",
        text="最终正文",
        save_history=False,
    )

    class _FailingTelegramModule:
        """
        模拟发送未确认的 Telegram 通知模块。
        """

        @staticmethod
        def get_priority() -> int:
            """
            返回模块调度优先级。
            """
            return 0

        @staticmethod
        def get_name() -> str:
            """
            返回模块展示名称。
            """
            return "Telegram"

        @staticmethod
        def post_message(message: Message, **_kwargs: object) -> None:
            """
            模拟 Telegram 模块未确认投递并抛错。
            """
            del message
            raise RuntimeError("Telegram 通知发送未确认：Telegram")

    with (
        patch.object(chain.eventmanager, "async_send_event", new_callable=AsyncMock),
        patch.object(
            chain._module_dispatcher._module_catalog,
            "get_running_modules",
            return_value=[_FailingTelegramModule()],
        ),
    ):
        with pytest.raises(RuntimeError, match="Telegram 通知发送未确认"):
            asyncio.run(chain.async_post_message(message, _strict_delivery=True))
        # 普通通知路径保持原有异常隔离语义，不影响其他后台通知调用方。
        asyncio.run(chain.async_post_message(message))
