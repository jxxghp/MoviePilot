import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.add_subscribe import AddSubscribeTool
from app.schemas.types import MessageChannel


class TestAgentAddSubscribeTool(unittest.TestCase):
    def test_tv_subscription_without_season_reports_default_first_season(self):
        tool = AddSubscribeTool(session_id="session-1", user_id="10001")
        tool.set_message_attr(
            channel=MessageChannel.Telegram.value,
            source="telegram-main",
            username="tg_display_name",
        )

        with patch(
            "app.agent.tools.impl.add_subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "")),
        ) as async_add, patch(
            "app.agent.tools.impl.add_subscribe.UserOper.get_name",
            return_value="moviepilot-user",
        ):
            result = asyncio.run(
                tool.run(
                    title="Breaking Bad",
                    year="2008",
                    media_type="tv",
                )
            )

        self.assertEqual(async_add.await_args.kwargs["username"], "moviepilot-user")
        self.assertIn("第1季", result)
        self.assertIn("默认按第一季订阅", result)

    def test_subscription_falls_back_to_channel_username_when_no_binding_exists(self):
        tool = AddSubscribeTool(session_id="session-1", user_id="10001")
        tool.set_message_attr(
            channel=MessageChannel.Telegram.value,
            source="telegram-main",
            username="tg_display_name",
        )

        with patch(
            "app.agent.tools.impl.add_subscribe.SubscribeChain.async_add",
            new=AsyncMock(return_value=(1, "")),
        ) as async_add, patch(
            "app.agent.tools.impl.add_subscribe.UserOper.get_name",
            return_value=None,
        ):
            result = asyncio.run(
                tool.run(
                    title="The Matrix",
                    year="1999",
                    media_type="movie",
                )
            )

        self.assertEqual(async_add.await_args.kwargs["username"], "tg_display_name")
        self.assertIn("成功添加订阅：The Matrix (1999)", result)


if __name__ == "__main__":
    unittest.main()
