import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent import _finish_processing_status
from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.modules.slack import SlackModule
from app.schemas.message import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageChannel


class TestMessageProcessingStatus(unittest.TestCase):
    def test_processing_status_capability_only_enabled_for_supported_channels(self):
        supported = {
            MessageChannel.Telegram,
            MessageChannel.Feishu,
            MessageChannel.Slack,
            MessageChannel.Discord,
        }

        for channel in MessageChannel:
            self.assertEqual(
                ChannelCapabilityManager.supports_capability(
                    channel, ChannelCapability.PROCESSING_STATUS
                ),
                channel in supported,
            )

    def test_slack_processing_status_uses_reaction(self):
        module = SlackModule()
        module._channel = MessageChannel.Slack
        client = MagicMock()
        client.add_reaction.return_value = True
        client.remove_reaction.return_value = True

        with (
            patch.object(
                module, "get_config", return_value=SimpleNamespace(name="slack-main")
            ),
            patch.object(module, "get_instance", return_value=client),
        ):
            status = module.mark_message_processing_started(
                channel=MessageChannel.Slack,
                source="slack-main",
                userid="U01",
                message_id="1710000000.000100",
                chat_id="C01",
                text="hello",
            )
            removed = module.mark_message_processing_finished(
                channel=MessageChannel.Slack,
                source="slack-main",
                userid="U01",
                status=status,
            )

        client.add_reaction.assert_called_once_with(
            channel="C01",
            timestamp="1710000000.000100",
            emoji="eyes",
        )
        client.remove_reaction.assert_called_once_with(
            channel="C01",
            timestamp="1710000000.000100",
            emoji="eyes",
        )
        self.assertEqual(status["metadata"]["kind"], "reaction")
        self.assertTrue(removed)

    def test_slack_parser_exposes_message_location_for_reaction_status(self):
        module = SlackModule()

        with patch.object(
            module, "get_config", return_value=SimpleNamespace(name="slack-main")
        ):
            message = module.message_parser(
                source="slack-main",
                body=json.dumps(
                    {
                        "type": "message",
                        "user": "U01",
                        "text": "hello",
                        "ts": "1710000000.000100",
                        "channel": "C01",
                    }
                ),
                form=None,
                args=None,
            )

        self.assertEqual(message.message_id, "1710000000.000100")
        self.assertEqual(message.chat_id, "C01")

    def test_discord_processing_status_starts_and_stops_typing(self):
        module = DiscordModule()
        module._channel = MessageChannel.Discord
        client = MagicMock()
        client.start_typing.return_value = True
        client.stop_typing.return_value = True

        with (
            patch.object(
                module, "get_config", return_value=SimpleNamespace(name="discord-main")
            ),
            patch.object(module, "get_instance", return_value=client),
        ):
            status = module.mark_message_processing_started(
                channel=MessageChannel.Discord,
                source="discord-main",
                userid="10001",
                message_id="20002",
                chat_id="30003",
                text="hello",
            )
            finished = module.mark_message_processing_finished(
                channel=MessageChannel.Discord,
                source="discord-main",
                userid="10001",
                status=status,
            )

        client.start_typing.assert_called_once_with(userid="10001", chat_id="30003")
        client.stop_typing.assert_called_once_with(userid="10001", chat_id="30003")
        self.assertEqual(status["metadata"]["kind"], "typing")
        self.assertTrue(finished)

    def test_agent_finish_processing_status_uses_module_interface(self):
        status = {
            "channel": MessageChannel.Telegram.value,
            "source": "telegram-main",
            "userid": "10001",
            "message_id": None,
            "chat_id": "-100",
            "metadata": {"kind": "typing"},
        }

        with patch("app.agent.AgentChain") as chain_cls:
            _finish_processing_status(status, user_id="fallback")

        chain_cls.return_value.finish_message_processing_status.assert_called_once_with(
            status=status,
            userid="fallback",
        )


class TestDiscordTypingLifecycle(IsolatedAsyncioTestCase):
    async def test_short_typing_task_can_stop_before_first_trigger(self):
        """
        短响应在首次 Discord typing 触发前结束时，不应留下客户端自然保留的输入状态。
        """
        discord_client = Discord.__new__(Discord)
        discord_client._loop = asyncio.get_running_loop()
        discord_client._typing_tasks = {}
        discord_client._typing_stop_events = {}
        discord_client._typing_interval_seconds = 0.01
        discord_client._typing_max_duration_seconds = 1
        channel = MagicMock()
        channel.trigger_typing = AsyncMock()

        with patch.object(discord_client, "_resolve_channel", return_value=channel):
            started = await discord_client._start_typing_task(
                typing_key="chat:30003",
                chat_id="30003",
                max_duration_seconds=1,
                initial_delay_seconds=0.05,
            )
            stopped = await discord_client._stop_typing_task("chat:30003")
            await asyncio.sleep(0.08)

        self.assertTrue(started)
        self.assertTrue(stopped)
        channel.trigger_typing.assert_not_called()
        self.assertNotIn("chat:30003", discord_client._typing_tasks)
