import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.agent import MoviePilotAgent
from app.agent.tools.impl.ask_user_choice import (
    AskUserChoiceTool,
    UserChoiceOptionInput,
)
from app.agent.tools.impl.send_message import SendMessageTool
from app.chain.message import MessageChain
from app.core.config import settings
from app.db import SessionFactory
from app.db.message_oper import MessageOper
from app.db.models.message import Message
from app.helper.interaction import AgentInteractionOption, agent_interaction_manager, media_interaction_manager
from app.schemas.types import MessageChannel, NotificationType


def _clear_messages() -> None:
    """清空消息表，隔离 Agent 消息路由测试数据。"""
    with SessionFactory() as db:
        db.query(Message).delete()
        db.commit()


def test_explicit_ai_message_bypasses_pending_media_interaction():
    """显式 /ai 消息应绕过误触发的媒体交互状态并回到 Agent 会话。"""
    chain = MessageChain()
    media_interaction_manager.clear()
    media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="确认",
        title="确认",
    )

    try:
        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.MediaInteractionChain.handle_text_interaction",
            return_value=True,
        ) as handle_media_interaction, patch.object(
            chain, "_handle_ai_message", return_value=True
        ) as handle_ai_message:
            chain.handle_message(
                channel=MessageChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="/ai 确认",
            )
    finally:
        media_interaction_manager.clear()

    handle_ai_message.assert_called_once()
    handle_media_interaction.assert_not_called()


def test_explicit_ai_message_is_not_recorded_to_message_history():
    """显式 /ai 消息不登记到数据库或实时消息队列。"""
    chain = MessageChain()

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        chain, "_record_user_message"
    ) as record_user_message, patch(
        "app.agent.agent_manager.process_message",
        new_callable=AsyncMock,
    ) as process_message, patch(
        "app.chain.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
    ):
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/ai 帮我检查订阅",
        )

    record_user_message.assert_not_called()
    process_message.assert_called_once()


def test_message_chain_passes_stable_channel_admin_principal_to_agent():
    """消息链应将渠道适配器生成的管理员事实传给 Agent。"""
    chain = MessageChain()

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch(
        "app.agent.agent_manager.process_message",
        new_callable=AsyncMock,
    ) as process_message, patch(
        "app.chain.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
    ):
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="renamed-user",
            is_channel_admin=True,
            text="/ai 检查系统状态",
        )

    assert process_message.call_args.kwargs["is_channel_admin"] is True


def test_message_chain_does_not_trust_channel_display_username():
    """消息链应保留适配器给出的明确非管理员结论。"""
    chain = MessageChain()

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch(
        "app.agent.agent_manager.process_message",
        new_callable=AsyncMock,
    ) as process_message, patch(
        "app.chain.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
    ):
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10002",
            username="admin",
            is_channel_admin=False,
            text="/ai 检查系统状态",
        )

    assert process_message.call_args.kwargs["is_channel_admin"] is False


def test_message_chain_uses_same_admin_contract_for_slack():
    """管理员事实透传应复用于其他消息渠道，而不是 Telegram 特判。"""
    chain = MessageChain()

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch(
        "app.agent.agent_manager.process_message",
        new_callable=AsyncMock,
    ) as process_message, patch(
        "app.chain.message.asyncio.run_coroutine_threadsafe",
        side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
    ):
        chain.handle_message(
            channel=MessageChannel.Slack,
            source="slack-test",
            userid="UADMIN",
            username="renamed-user",
            is_channel_admin=True,
            text="/ai 检查系统状态",
        )

    assert process_message.call_args.kwargs["is_channel_admin"] is True


def test_ask_user_choice_message_is_not_recorded_to_message_history():
    """Agent 询问用户意图工具发送的按钮消息不登记到消息表。"""
    _clear_messages()
    tool = AskUserChoiceTool(session_id="session-choice", user_id="10001")
    tool.set_message_attr(
        channel=MessageChannel.Telegram.value,
        source="telegram-test",
        username="tester",
    )
    tool.set_agent_context(agent_context={})

    try:
        with patch(
            "app.core.event.EventManager.async_send_event",
            new_callable=AsyncMock,
        ) as async_send_event, patch(
            "app.helper.message.MessageQueueManager.async_send_message",
            new_callable=AsyncMock,
        ) as async_send_message:
            result = asyncio.run(
                tool.run(
                    message="请选择要执行的操作",
                    options=[
                        UserChoiceOptionInput(label="继续下载", value="继续下载"),
                        UserChoiceOptionInput(label="先看详情", value="先看详情"),
                    ],
                    title="需要你的选择",
                )
            )
    finally:
        agent_interaction_manager.clear()

    assert "等待用户选择" in result
    assert tool._agent_context.get("user_reply_sent") is True
    assert MessageOper().list_by_page(page=1, count=10) == []
    async_send_event.assert_awaited_once()
    async_send_message.assert_awaited_once()


def test_agent_final_reply_disables_notification_history():
    """Agent 最终回复发往渠道时不保存通知历史。"""
    agent = MoviePilotAgent(
        session_id="session-agent-reply",
        user_id="10001",
        channel=MessageChannel.Telegram.value,
        source="telegram-test",
        username="tester",
    )

    with patch(
        "app.agent.AgentChain.async_post_message",
        new_callable=AsyncMock,
    ) as async_post_message:
        asyncio.run(agent.send_agent_message("已完成处理"))

    notification = async_post_message.await_args.args[0]
    assert notification.mtype == NotificationType.Agent
    assert notification.save_history is False


def test_send_message_tool_disables_notification_history():
    """Agent 主动发消息工具发送的通知不保存通知历史。"""
    tool = SendMessageTool(session_id="session-send-message", user_id="10001")
    tool.set_message_attr(
        channel=MessageChannel.Telegram.value,
        source="telegram-test",
        username="tester",
    )
    tool.set_agent_context(agent_context={})

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new_callable=AsyncMock,
    ) as async_post_message:
        result = asyncio.run(tool.run(message="处理结果", title="MoviePilot助手"))

    notification = async_post_message.await_args.args[0]
    assert result == "消息已发送"
    assert notification.text == "处理结果"
    assert notification.save_history is False


def test_agent_choice_callback_is_not_recorded_to_message_history():
    """Agent 按钮选择回传不登记到数据库或实时消息队列。"""
    chain = MessageChain()
    request = agent_interaction_manager.create_request(
        session_id="session-choice",
        user_id="10001",
        channel=MessageChannel.Telegram.value,
        source="telegram-test",
        username="tester",
        title="需要你的选择",
        prompt="请选择",
        options=[
            AgentInteractionOption(label="电影", value="我选择电影"),
            AgentInteractionOption(label="电视剧", value="我选择电视剧"),
        ],
    )

    try:
        with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
            chain, "_record_user_message"
        ) as record_user_message, patch.object(
            chain, "edit_message", return_value=True
        ), patch(
            "app.agent.agent_manager.process_message",
            new_callable=AsyncMock,
        ) as process_message, patch(
            "app.chain.message.asyncio.run_coroutine_threadsafe",
            side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
        ):
            chain._handle_callback(
                text=f"CALLBACK:agent_interaction:choice:{request.request_id}:1",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                is_channel_admin=False,
                original_message_id=123,
                original_chat_id="456",
            )
    finally:
        agent_interaction_manager.clear()

    record_user_message.assert_not_called()
    process_message.assert_called_once()
    assert process_message.call_args.kwargs["is_channel_admin"] is False
