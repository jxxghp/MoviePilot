"""
验证按钮选择工具、反馈门禁及智能体交互回调。
"""

import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.agent.prompt import prompt_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.ask_user_choice import (
    AskUserChoiceTool,
    UserChoiceOptionInput,
)
from app.agent.tools.impl.send_message import SendMessageTool
from app.application.messaging.interaction import InteractionContext
from app.application.messaging.interaction.agent import (
    AgentInteractionOption,
    agent_interaction_manager,
)
from app.chain.message import MessageChain
from app.runtime.config import settings
from app.runtime.loop import main_loop_registry
from app.schemas.types import NotificationChannel


@pytest.fixture(autouse=True)
def _isolate_test_state():
    """
    隔离测试之间共享的会话与交互状态，并在结束时清理。
    """
    try:
        yield
    finally:
        agent_interaction_manager.clear()


def test_prompt_injects_choice_tool_hint_only_for_button_channels() -> None:
    """
    仅支持按钮的渠道注入选择工具的交互说明。
    """
    telegram_prompt = prompt_manager.get_agent_prompt(channel=NotificationChannel.Telegram.value)
    web_agent_prompt = prompt_manager.get_agent_prompt(channel=NotificationChannel.WebAgent.value)
    wechat_prompt = prompt_manager.get_agent_prompt(channel=NotificationChannel.Wechat.value)

    assert "ask_user_choice" in telegram_prompt
    assert "ask_user_choice" in web_agent_prompt
    assert "terminal interaction tool" in telegram_prompt
    assert "do not write a final text reply after it" in telegram_prompt
    assert "ask_user_choice" not in wechat_prompt


def test_prompt_does_not_inject_send_message_html_hint() -> None:
    """
    提示词不再注入旧版 HTML 消息发送说明。
    """
    telegram_prompt = prompt_manager.get_agent_prompt(channel=NotificationChannel.Telegram.value)
    wechat_prompt = prompt_manager.get_agent_prompt(channel=NotificationChannel.Wechat.value)

    assert 'parse_mode="HTML"' not in telegram_prompt
    assert "Telegram-supported HTML tags" not in telegram_prompt
    assert 'parse_mode="HTML"' not in wechat_prompt


def test_factory_injects_choice_tool_only_for_button_channels() -> None:
    """
    工具工厂仅向支持按钮的渠道提供选择工具。
    """
    with patch(
        "app.agent.tools.factory._get_plugin_agent_tools",
        return_value=[],
    ):
        telegram_tools = MoviePilotToolFactory.create_tools(
            session_id="session-1",
            user_id="10001",
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        web_agent_tools = MoviePilotToolFactory.create_tools(
            session_id="session-web",
            user_id="10001",
            channel=NotificationChannel.WebAgent.value,
            source="web-agent",
            username="tester",
        )
        wechat_tools = MoviePilotToolFactory.create_tools(
            session_id="session-2",
            user_id="10001",
            channel=NotificationChannel.Wechat.value,
            source="wechat-test",
            username="tester",
        )

    assert "ask_user_choice" in [tool.name for tool in telegram_tools]
    assert "ask_user_choice" in [tool.name for tool in web_agent_tools]
    assert "ask_user_choice" not in [tool.name for tool in wechat_tools]


def test_choice_tool_returns_direct_after_sending_interaction() -> None:
    """
    发送按钮后应结束当前 Agent 轮次，等待用户选择作为新消息进入。
    """
    tool = AskUserChoiceTool(session_id="session-1", user_id="10001")

    assert tool.return_direct
    assert "本工具结束当前交互轮次" in tool.description


def test_send_message_tool_returns_direct_after_sending_message() -> None:
    """
    发送消息工具发出用户可见消息后应结束当前 Agent 轮次。
    """
    tool = SendMessageTool(session_id="session-1", user_id="10001")

    assert tool.return_direct
    assert "本工具直接完成用户回复" in tool.description


def test_choice_tool_sends_buttons_and_registers_pending_request() -> None:
    """
    选择工具发送完整按钮并登记待处理请求。
    """
    tool = AskUserChoiceTool(session_id="session-1", user_id="10001")
    tool.set_message_attr(
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
        username="tester",
    )
    tool.set_agent_context(agent_context={})

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new=AsyncMock(),
    ) as async_post_message:
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

    assert "等待用户选择" in result
    assert tool._agent_context.get("user_reply_sent")
    notification = async_post_message.await_args.args[0]
    assert notification.text == "请选择要执行的操作"
    assert sum((len(row) for row in notification.buttons)) == 2
    assert "description" not in notification.buttons[0][0]

    callback_data = notification.buttons[0][0]["callback_data"]
    _, _, request_id, option_index = callback_data.split(":")
    resolved = agent_interaction_manager.resolve(request_id, int(option_index), "10001")
    assert resolved is not None
    _, option = resolved
    assert option.value == "继续下载"


def test_choice_tool_blocks_after_feedback_quality_rejection() -> None:
    """
    反馈质量被拒绝后阻止继续发送选择请求。
    """
    tool = AskUserChoiceTool(session_id="session-feedback", user_id="10001")
    tool.set_message_attr(
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
        username="tester",
    )
    tool.set_agent_context(agent_context={"feedback_issue_rejected_quality": True})

    with patch(
        "app.agent.tools.base.ToolChain.async_post_message",
        new=AsyncMock(),
    ) as async_post_message:
        result = asyncio.run(
            tool.run(
                message="测试ISSUE提交被系统质量校验拦截，请选择：",
                options=[
                    UserChoiceOptionInput(
                        label="提供真实问题描述重新提交",
                        value="提供真实问题描述重新提交",
                    ),
                    UserChoiceOptionInput(
                        label="取消测试，了解原因",
                        value="取消测试，了解原因",
                    ),
                ],
            )
        )

    assert "质量门槛拒绝" in result
    async_post_message.assert_not_awaited()


def test_agent_interaction_callback_routes_selected_value_back_to_agent() -> None:
    """
    按钮回调将所选值作为用户输入送回智能体。
    """
    chain = MessageChain()
    chain.runtime_config = replace(
        chain.runtime_config,
        ai_agent_enable=True,
    )
    request = agent_interaction_manager.create_request(
        session_id="session-choice",
        user_id="10001",
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
        username="tester",
        title="需要你的选择",
        prompt="请选择",
        options=[
            AgentInteractionOption(label="电影", value="我选择电影"),
            AgentInteractionOption(label="电视剧", value="我选择电视剧"),
        ],
    )

    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    with (
        patch.object(main_loop_registry, "require", return_value=loop),
        patch.object(settings, "AI_AGENT_ENABLE", True),
        patch.object(chain.messagehelper, "put") as message_put,
        patch.object(chain.messageoper, "add") as message_add,
        patch.object(chain, "edit_message", return_value=True) as edit_message,
        patch("app.chain.message.get_running_agent_manager") as get_running_manager,
        patch(
            "app.chain.message.asyncio.run_coroutine_threadsafe",
            side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
        ),
    ):
        process_message = AsyncMock()
        get_running_manager.return_value.process_message = process_message
        handled = chain._handle_callback(
            callback_data=f"agent_interaction:choice:{request.request_id}:1",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
                original_message_id=123,
                original_chat_id="456",
            ),
        )

    assert handled
    edit_message.assert_called_once_with(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        message_id=123,
        chat_id="456",
        title="需要你的选择",
        text="请选择\n\n已选择：电影",
    )
    process_message.assert_called_once()
    kwargs = process_message.call_args.kwargs
    assert kwargs["message"] == "我选择电影"
    assert kwargs["session_id"] == "session-choice"
    assert kwargs["channel"] == NotificationChannel.Telegram.value
    assert kwargs["source"] == "telegram-test"
    assert "processing_status" not in kwargs
    message_put.assert_not_called()
    message_add.assert_not_called()


def test_legacy_agent_choice_callback_still_supported() -> None:
    """
    旧版选择按钮回调仍按兼容路径处理。
    """
    chain = MessageChain()
    request = agent_interaction_manager.create_request(
        session_id="session-choice",
        user_id="10001",
        channel=NotificationChannel.Telegram.value,
        source="telegram-test",
        username="tester",
        title=None,
        prompt="请选择",
        options=[AgentInteractionOption(label="电影", value="我选择电影")],
    )

    with (
        patch.object(chain, "_handle_ai_message") as handle_ai_message,
        patch.object(chain.messagehelper, "put"),
        patch.object(chain.messageoper, "add"),
    ):
        chain._handle_callback(
            callback_data=f"agent_choice:{request.request_id}:1",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
            ),
        )

    handle_ai_message.assert_called_once()


def test_secret_confirmation_preempts_plugin_interaction_on_message_channels() -> None:
    """
    TG/飞书确认必须回到已有 Agent 会话，不被其它输入会话消费。
    """
    chain = MessageChain()

    try:
        for channel in (NotificationChannel.Telegram, NotificationChannel.Feishu):
            key = chain._session_user_key("10001", channel, f"{channel.value}-test")
            MessageChain._user_sessions[key] = ("session-secret", datetime.now())
            manager = Mock()
            manager.matches_secret_confirmation.return_value = True
            with (
                patch(
                    "app.chain.message.get_running_agent_manager",
                    return_value=manager,
                ),
                patch.object(
                    chain,
                    "_handle_ai_message",
                    return_value=True,
                ) as handle_ai_message,
                patch(
                    "app.chain.message.PluginInputInteractionHandler.handle_text",
                ) as handle_plugin_interaction,
                patch.object(
                    chain,
                    "_mark_message_processing_started",
                ) as mark_processing_started,
            ):
                chain.handle_message(
                    channel=channel,
                    source=f"{channel.value}-test",
                    userid="10001",
                    username="tester",
                    text="确认",
                    original_message_id="message-1",
                    original_chat_id="chat-1",
                    images=None,
                    audio_refs=None,
                    files=None,
                )

            handle_ai_message.assert_called_once()
            handle_plugin_interaction.assert_not_called()
            mark_processing_started.assert_not_called()
            assert handle_ai_message.call_args.kwargs["session_id"] == "session-secret"
    finally:
        MessageChain._user_sessions.clear()
