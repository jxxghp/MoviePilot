import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.prompt import prompt_manager
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.ask_user_choice import (
    AskUserChoiceTool,
    UserChoiceOptionInput,
)
from app.agent.tools.impl.feedback_issue_state import (
    FEEDBACK_CONFIRM_VALUE_PREFIX,
    build_feedback_draft_hash,
    feedback_issue_state_store,
)
from app.helper.interaction import (
    AgentInteractionOption,
    agent_interaction_manager,
)
from app.chain.message import MessageChain
from app.schemas.types import MessageChannel


class TestAgentInteraction(unittest.TestCase):
    def tearDown(self):
        agent_interaction_manager.clear()
        feedback_issue_state_store.clear()

    def test_prompt_injects_choice_tool_hint_only_for_button_channels(self):
        telegram_prompt = prompt_manager.get_agent_prompt(
            channel=MessageChannel.Telegram.value
        )
        wechat_prompt = prompt_manager.get_agent_prompt(
            channel=MessageChannel.Wechat.value
        )

        self.assertIn("ask_user_choice", telegram_prompt)
        self.assertNotIn("ask_user_choice", wechat_prompt)

    def test_factory_injects_choice_tool_only_for_button_channels(self):
        with patch(
            "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
            return_value=[],
        ):
            telegram_tools = MoviePilotToolFactory.create_tools(
                session_id="session-1",
                user_id="10001",
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                username="tester",
            )
            wechat_tools = MoviePilotToolFactory.create_tools(
                session_id="session-2",
                user_id="10001",
                channel=MessageChannel.Wechat.value,
                source="wechat-test",
                username="tester",
            )

        self.assertIn("ask_user_choice", [tool.name for tool in telegram_tools])
        self.assertNotIn("ask_user_choice", [tool.name for tool in wechat_tools])

    def test_choice_tool_sends_buttons_and_registers_pending_request(self):
        tool = AskUserChoiceTool(session_id="session-1", user_id="10001")
        tool.set_message_attr(
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        tool.set_agent_context(agent_context={})

        with patch(
            "app.agent.tools.impl.ask_user_choice.ToolChain.async_post_message",
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
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        tool.set_agent_context(
            agent_context={"feedback_issue_rejected_quality": True}
        )

        with patch(
            "app.agent.tools.impl.ask_user_choice.ToolChain.async_post_message",
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

    def test_choice_tool_blocks_after_feedback_preview_pending(self):
        """#5807 回归：prepare_feedback_issue 发完按钮后，agent 不应再叠 ask_user_choice。

        否则用户会收到两个确认按钮、点两次、agent 跑两轮 → 同一条成功
        文案在 TG 里重复 3 次。"""
        tool = AskUserChoiceTool(session_id="session-feedback", user_id="10001")
        tool.set_message_attr(
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
        )
        tool.set_agent_context(
            agent_context={"reply_mode": "feedback_issue_confirmation"}
        )

        with patch(
            "app.agent.tools.impl.ask_user_choice.ToolChain.async_post_message",
            new=AsyncMock(),
        ) as async_post_message:
            result = asyncio.run(
                tool.run(
                    message="已准备 ISSUE，请确认是否提交到上游仓库？",
                    options=[
                        UserChoiceOptionInput(label="确认提交", value="确认提交"),
                        UserChoiceOptionInput(label="取消", value="取消"),
                    ],
                )
            )

        # 工具应该自我拒绝，不再发第二个按钮卡片
        self.assertIn("prepare_feedback_issue", result)
        async_post_message.assert_not_awaited()

    def test_agent_interaction_callback_routes_selected_value_back_to_agent(self):
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

        with patch.object(chain, "_handle_ai_message") as handle_ai_message, patch.object(
            chain.messagehelper, "put"
        ) as message_put, patch.object(chain.messageoper, "add") as message_add, patch.object(
            chain, "edit_message", return_value=True
        ) as edit_message:
            chain._handle_callback(
                text=f"CALLBACK:agent_interaction:choice:{request.request_id}:1",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                original_message_id=123,
                original_chat_id="456",
            )

        handle_ai_message.assert_called_once()
        edit_message.assert_called_once_with(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            message_id=123,
            chat_id="456",
            title="需要你的选择",
            text="请选择\n\n已选择：电影",
        )
        kwargs = handle_ai_message.call_args.kwargs
        self.assertEqual(kwargs["text"], "我选择电影")
        self.assertEqual(kwargs["session_id"], "session-choice")
        message_put.assert_called_once()
        message_add.assert_called_once()

    def test_feedback_confirmation_callback_marks_token_confirmed(self):
        draft_hash = build_feedback_draft_hash(
            title="[错误报告]: 订阅刷新接口返回 500 错误码",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="## 现象\n错误\n## 复现步骤\n点击刷新\n## 期望行为\n正常刷新",
            original_user_request="订阅刷新接口返回 500",
            logs="ERROR demo",
            diagnostics_id="diag-1",
        )
        confirmation = feedback_issue_state_store.create_confirmation(
            session_id="session-feedback",
            user_id="10001",
            username="tester",
            draft_hash=draft_hash,
            diagnostics_id="diag-1",
        )
        request = agent_interaction_manager.create_request(
            session_id="session-feedback",
            user_id="10001",
            channel=MessageChannel.Telegram.value,
            source="telegram-test",
            username="tester",
            title="确认提交问题反馈",
            prompt="请确认",
            options=[
                AgentInteractionOption(
                    label="确认提交",
                    value=f"{FEEDBACK_CONFIRM_VALUE_PREFIX}{confirmation.confirmation_token}",
                )
            ],
        )
        chain = MessageChain()

        with patch.object(chain, "_handle_ai_message") as handle_ai_message, patch.object(
            chain.messagehelper, "put"
        ), patch.object(chain.messageoper, "add"), patch.object(
            chain, "edit_message", return_value=True
        ):
            chain._handle_callback(
                text=f"CALLBACK:agent_interaction:choice:{request.request_id}:1",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        kwargs = handle_ai_message.call_args.kwargs
        self.assertIn("confirmation_token", kwargs["text"])
        consumed = feedback_issue_state_store.consume_confirmed(
            confirmation.confirmation_token,
            session_id="session-feedback",
            user_id="10001",
            draft_hash=draft_hash,
        )
        self.assertIsNotNone(consumed)

    def test_state_store_active_confirmation_helpers(self):
        # find_active_confirmation 应只返回 confirmed_at=None 的记录
        rec1 = feedback_issue_state_store.create_confirmation(
            session_id="s1", user_id="u1", username=None,
            draft_hash="h1", diagnostics_id="d1",
        )
        rec2 = feedback_issue_state_store.create_confirmation(
            session_id="s1", user_id="u2", username=None,
            draft_hash="h2", diagnostics_id="d2",
        )
        # 跨用户隔离
        self.assertEqual(
            feedback_issue_state_store.find_active_confirmation(
                session_id="s1", user_id="u1"
            ).confirmation_token,
            rec1.confirmation_token,
        )
        # 标记为已确认后不应再被 active 检索返回
        feedback_issue_state_store.mark_confirmed(
            rec1.confirmation_token, session_id="s1", user_id="u1"
        )
        self.assertIsNone(
            feedback_issue_state_store.find_active_confirmation(
                session_id="s1", user_id="u1"
            )
        )
        # invalidate_active_confirmations 只清掉当前会话+用户的 pending 记录
        dropped = feedback_issue_state_store.invalidate_active_confirmations(
            session_id="s1", user_id="u2"
        )
        self.assertEqual(dropped, 1)
        self.assertIsNone(
            feedback_issue_state_store.find_active_confirmation(
                session_id="s1", user_id="u2"
            )
        )
        # 已 confirmed 的 rec1 不应该被这次 invalidate 误删
        self.assertIn(rec1.confirmation_token, feedback_issue_state_store._confirmations)

    def test_legacy_agent_choice_callback_still_supported(self):
        chain = MessageChain()
        request = agent_interaction_manager.create_request(
            session_id="session-choice",
            user_id="10001",
            channel=MessageChannel.Telegram.value,
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
                text=f"CALLBACK:agent_choice:{request.request_id}:1",
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

        handle_ai_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
