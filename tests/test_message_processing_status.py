import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agent.orchestrator import _finish_processing_status
from app.modules.discord import DiscordModule
from app.modules.slack import SlackModule
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import NotificationChannel


def test_processing_status_capability_only_enabled_for_supported_channels() -> None:
    supported = {
        NotificationChannel.Telegram,
        NotificationChannel.Feishu,
        NotificationChannel.Slack,
        NotificationChannel.Discord,
    }

    for channel in NotificationChannel:
        assert ChannelCapabilityManager.supports_capability(
            channel, ChannelCapability.PROCESSING_STATUS
        ) is (channel in supported)


def test_slack_processing_status_uses_reaction() -> None:
    module = SlackModule()
    module._channel = NotificationChannel.Slack
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
            channel=NotificationChannel.Slack,
            source="slack-main",
            userid="U01",
            message_id="1710000000.000100",
            chat_id="C01",
            text="hello",
        )
        removed = module.mark_message_processing_finished(
            channel=NotificationChannel.Slack,
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
    assert status["metadata"]["kind"] == "reaction"
    assert removed


def test_slack_parser_exposes_message_location_for_reaction_status() -> None:
    module = SlackModule()

    with patch.object(
        module,
        "get_config",
        return_value=SimpleNamespace(name="slack-main", config={}),
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

    assert message.message_id == "1710000000.000100"
    assert message.chat_id == "C01"


def test_discord_processing_status_starts_and_stops_typing() -> None:
    module = DiscordModule()
    module._channel = NotificationChannel.Discord
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
            channel=NotificationChannel.Discord,
            source="discord-main",
            userid="10001",
            message_id="20002",
            chat_id="30003",
            text="hello",
        )
        finished = module.mark_message_processing_finished(
            channel=NotificationChannel.Discord,
            source="discord-main",
            userid="10001",
            status=status,
        )

    client.start_typing.assert_called_once_with(userid="10001", chat_id="30003")
    client.stop_typing.assert_called_once_with(userid="10001", chat_id="30003")
    assert status["metadata"]["kind"] == "typing"
    assert finished


def test_agent_finish_processing_status_uses_module_interface() -> None:
    status = {
        "channel": NotificationChannel.Telegram.value,
        "source": "telegram-main",
        "userid": "10001",
        "message_id": None,
        "chat_id": "-100",
        "metadata": {"kind": "typing"},
    }

    with patch("app.agent.orchestrator.AgentChain") as chain_cls:
        _finish_processing_status(status, user_id="fallback")

    chain_cls.return_value.finish_message_processing_status.assert_called_once_with(
        status=status,
        userid="fallback",
    )
