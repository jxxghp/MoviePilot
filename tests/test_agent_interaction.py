import asyncio
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from app.agent.prompt import prompt_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.ask_user_choice import (
    AskUserChoiceTool,
    UserChoiceOptionInput,
)
from app.agent.tools.impl.send_message import SendMessageTool
from app.application.messaging.agent import (
    AgentInteractionOption,
    agent_interaction_manager,
)
from app.application.messaging.interaction import InteractionContext
from app.application.orchestration.message import MessageChain
from app.runtime.config import settings
from app.schemas.types import NotificationChannel


class TestAgentInteraction(unittest.TestCase):
    def tearDown(self):
        agent_interaction_manager.clear()

    def test_prompt_injects_choice_tool_hint_only_for_button_channels(self):
        telegram_prompt = prompt_manager.get_agent_prompt(
            channel=NotificationChannel.Telegram.value
        )
        web_agent_prompt = prompt_manager.get_agent_prompt(
            channel=NotificationChannel.WebAgent.value
        )
        wechat_prompt = prompt_manager.get_agent_prompt(
            channel=NotificationChannel.Wechat.value
        )

        self.assertIn("ask_user_choice", telegram_prompt)
        self.assertIn("ask_user_choice", web_agent_prompt)
        self.assertIn("terminal interaction tool", telegram_prompt)
        self.assertIn("do not write a final text reply after it", telegram_prompt)
        self.assertNotIn("ask_user_choice", wechat_prompt)

    def test_prompt_does_not_inject_send_message_html_hint(self):
        telegram_prompt = prompt_manager.get_agent_prompt(
            channel=NotificationChannel.Telegram.value
        )
        wechat_prompt = prompt_manager.get_agent_prompt(
            channel=NotificationChannel.Wechat.value
        )

        self.assertNotIn("parse_mode=\"HTML\"", telegram_prompt)
        self.assertNotIn("Telegram-supported HTML tags", telegram_prompt)
        self.assertNotIn("parse_mode=\"HTML\"", wechat_prompt)

    def test_factory_injects_choice_tool_only_for_button_channels(self):
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

        self.assertIn("ask_user_choice", [tool.name for tool in telegram_tools])
        self.assertIn("ask_user_choice", [tool.name for tool in web_agent_tools])
        self.assertNotIn("ask_user_choice", [tool.name for tool in wechat_tools])

    def test_choice_tool_returns_direct_after_sending_interaction(self):
        """发送按钮后应结束当前 Agent 轮次，等待用户选择作为新消息进入。"""
        tool = AskUserChoiceTool(session_id="session-1", user_id="10001")

        self.assertTrue(tool.return_direct)
        self.assertIn("terminal interaction tool", tool.description)

    def test_send_message_tool_returns_direct_after_sending_message(self):
        """发送消息工具发出用户可见消息后应结束当前 Agent 轮次。"""
        tool = SendMessageTool(session_id="session-1", user_id="10001")

        self.assertTrue(tool.return_direct)
        self.assertIn("terminal response tool", tool.description)

    def test_choice_tool_sends_buttons_and_registers_pending_request(self):
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

        self.assertIn("等待用户选择", result)
        self.assertTrue(tool._agent_context.get("user_reply_sent"))
        notification = async_post_message.await_args.args[0]
        self.assertEqual(notification.text, "请选择要执行的操作")
        self.assertEqual(sum(len(row) for row in notification.buttons), 2)
        self.assertNotIn("description", notification.buttons[0][0])

        callback_data = notification.buttons[0][0]["callback_data"]
        _, _, request_id, option_index = callback_data.split(":")
        resolved = agent_interaction_manager.resolve(
            request_id, int(option_index), "10001"
        )
        self.assertIsNotNone(resolved)
        _, option = resolved
        self.assertEqual(option.value, "继续下载")

    def test_choice_tool_blocks_after_feedback_quality_rejection(self):
        tool = AskUserChoiceTool(session_id="session-feedback", user_id="10001")
        tool.set_message_attr(
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        tool.set_agent_context(
            agent_context={"feedback_issue_rejected_quality": True}
        )

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

        self.assertIn("质量门槛拒绝", result)
        async_post_message.assert_not_awaited()

    def test_agent_interaction_callback_routes_selected_value_back_to_agent(self):
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

        with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
            chain.messagehelper, "put"
        ) as message_put, patch.object(
            chain.messageoper, "add"
        ) as message_add, patch.object(
            chain, "edit_message", return_value=True
        ) as edit_message, patch(
            "app.application.orchestration.message.get_running_agent_manager"
        ) as get_running_manager, patch(
            "app.application.orchestration.message.asyncio.run_coroutine_threadsafe",
            side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
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

        self.assertTrue(handled)
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
        self.assertEqual(kwargs["message"], "我选择电影")
        self.assertEqual(kwargs["session_id"], "session-choice")
        self.assertEqual(kwargs["channel"], NotificationChannel.Telegram.value)
        self.assertEqual(kwargs["source"], "telegram-test")
        self.assertNotIn("processing_status", kwargs)
        message_put.assert_not_called()
        message_add.assert_not_called()

    def test_legacy_agent_choice_callback_still_supported(self):
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

        with patch.object(chain, "_handle_ai_message") as handle_ai_message, patch.object(
            chain.messagehelper, "put"
        ), patch.object(chain.messageoper, "add"):
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

    def test_secret_confirmation_preempts_plugin_interaction_on_message_channels(self):
        """TG/飞书确认必须回到已有 Agent 会话，不被其它输入会话消费。"""
        chain = MessageChain()
        MessageChain._user_sessions["10001"] = ("session-secret", datetime.now())

        try:
            for channel in (NotificationChannel.Telegram, NotificationChannel.Feishu):
                manager = Mock()
                manager.matches_secret_confirmation.return_value = True
                with patch(
                    "app.application.orchestration.message.get_running_agent_manager",
                    return_value=manager,
                ), patch.object(
                    chain,
                    "_handle_ai_message",
                    return_value=True,
                ) as handle_ai_message, patch(
                    "app.application.orchestration.message.PluginInputInteractionHandler.handle_text",
                ) as handle_plugin_interaction, patch.object(
                    chain,
                    "_mark_message_processing_started",
                ) as mark_processing_started:
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
                self.assertEqual(
                    handle_ai_message.call_args.kwargs["session_id"],
                    "session-secret",
                )
        finally:
            MessageChain._user_sessions.clear()
