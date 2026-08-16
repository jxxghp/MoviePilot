import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.schemas import CommandRegisterEventData


def test_discord_module_register_commands_filters_event_subset():
    """Discord 模块注册命令时应复用渠道级 CommandRegister 事件过滤结果。"""
    module = DiscordModule()
    client = SimpleNamespace(register_commands=Mock(), delete_commands=Mock())
    original_commands = {
        "/sites": {"description": "管理站点"},
        "/version": {"description": "当前版本"},
    }
    event = SimpleNamespace(
        event_data=CommandRegisterEventData(
            commands={"/sites": {"description": "管理站点"}, "/unknown": {"description": "无效"}},
            origin="DiscordFilter",
            service="discord-main",
        )
    )

    with (
        patch.object(
            module,
            "get_configs",
            return_value={"discord-main": SimpleNamespace(name="discord-main", config={})},
        ),
        patch.object(module, "get_instance", return_value=client),
        patch("app.modules._base.notification.eventmanager.send_event", return_value=event),
    ):
        module.register_commands(original_commands)

    client.register_commands.assert_called_once_with(
        {"/sites": {"description": "管理站点"}}
    )
    client.delete_commands.assert_not_called()


def test_discord_module_register_commands_deletes_when_event_canceled():
    """Discord 模块注册命令被事件取消时应清理应用命令。"""
    module = DiscordModule()
    client = SimpleNamespace(register_commands=Mock(), delete_commands=Mock())
    event = SimpleNamespace(
        event_data=CommandRegisterEventData(
            commands={"/sites": {"description": "管理站点"}},
            origin="DiscordFilter",
            service="discord-main",
            cancel=True,
        )
    )

    with (
        patch.object(
            module,
            "get_configs",
            return_value={"discord-main": SimpleNamespace(name="discord-main", config={})},
        ),
        patch.object(module, "get_instance", return_value=client),
        patch("app.modules._base.notification.eventmanager.send_event", return_value=event),
    ):
        module.register_commands({"/sites": {"description": "管理站点"}})

    client.delete_commands.assert_called_once_with()
    client.register_commands.assert_not_called()


def test_discord_normalizes_slash_command_names():
    """Discord 命令名称应符合平台只允许小写字母数字下划线连字符的约束。"""
    assert Discord._normalize_slash_command_name("/sites") == "sites"
    assert Discord._normalize_slash_command_name("/clear_cache") == "clear_cache"
    assert Discord._normalize_slash_command_name("/INVALID") == "invalid"
    assert Discord._normalize_slash_command_name("/中文") == ""
    assert Discord._normalize_slash_command_name("/" + "a" * 33) == ""


def test_discord_handle_slash_command_forwards_to_message_chain():
    """Discord 斜杠命令回调应转发为统一消息入口可识别的命令文本。"""
    client = Discord.__new__(Discord)
    client._update_user_chat_mapping = Mock()
    client._post_to_ds = AsyncMock()

    user = SimpleNamespace(id=10001, display_name="tester", global_name=None, name="tester")
    channel = SimpleNamespace(id=20001)
    interaction = SimpleNamespace(
        id=30001,
        user=user,
        channel=channel,
        response=SimpleNamespace(
            defer=AsyncMock(),
            is_done=Mock(return_value=True),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    asyncio.run(client._handle_slash_command(interaction, "/sites", "refresh"))

    client._update_user_chat_mapping.assert_called_once_with("10001", "20001")
    client._post_to_ds.assert_awaited_once_with(
        {
            "type": "message",
            "userid": "10001",
            "username": "tester",
            "user_tag": str(user),
            "text": "/sites refresh",
            "message_id": "30001",
            "chat_id": "20001",
            "channel_type": "guild",
        }
    )
    interaction.followup.send.assert_awaited_once_with(
        "命令已提交，请稍等...",
        ephemeral=True,
    )
