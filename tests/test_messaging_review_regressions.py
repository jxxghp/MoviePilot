"""
消息渠道审查发现的真实路由、身份与文本边界回归，外部渠道均用替身隔离。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import notification as notification_endpoint
from app.api.endpoints.notification import _normalize_configs
from app.chain.music_interaction.music import resolve_media_action
from app.chain.system import SystemChain
from app.domain.context import MediaInfo
from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("available", [True, False])
def test_discord_explicit_recipient_never_uses_broadcast(available):
    """
    指定用户的通知只能到私聊；DM不可用时明确失败，不回落公共频道。
    """
    client = object.__new__(Discord)
    private = object() if available else None
    client._broadcast_channel = object()
    client._channel_id = 456
    client._guild_id = None
    client._get_user_chat_id = Mock(return_value=None)
    client._get_dm_channel = AsyncMock(return_value=private)
    client._client = Mock()
    assert asyncio.run(client._resolve_channel(userid="123")) is private
    client._get_dm_channel.assert_awaited_once_with("123")


@pytest.mark.parametrize("result,expected", [((False, None), False), ((True, {}), True), (False, False)])
def test_discord_edit_reports_tuple_status(monkeypatch, result, expected):
    """
    编辑返回失败tuple时不能凭容器真值误报成功。
    """
    module = object.__new__(DiscordModule)
    module._channel = NotificationChannel.Discord
    monkeypatch.setattr(module, "get_configs", lambda: {"review": SimpleNamespace(name="review")})
    monkeypatch.setattr(module, "get_instance", lambda _name: SimpleNamespace(send_msg=lambda **_kw: result))
    assert module.edit_message(NotificationChannel.Discord, "review", "1", "2", "text") is expected


def test_slack_slash_command_preserves_arguments(monkeypatch):
    """
    原生命令和text参数一起传给统一命令处理器。
    """
    module = object.__new__(SlackModule)
    monkeypatch.setattr(module, "get_config", lambda _source: SimpleNamespace(name="review", config={}))
    monkeypatch.setattr(module, "get_instance", lambda _name: Mock())
    monkeypatch.setattr(module, "_should_reject_admin_command", lambda *_args: False)
    message = module.message_parser("review", json.dumps({"command": "/search", "text": "Review title",
        "user_id": "U1", "user_name": "review", "channel_id": "C1"}), None, None)
    assert message.text == "/search Review title"


def test_slack_media_buttons_keep_original_positions():
    """
    前项无海报时，后项卡片仍对应原列表第二项。
    """
    client = object.__new__(Slack)
    client._client = Mock()
    media = [MediaInfo(title="No poster"), MediaInfo(title="With poster", poster_path="https://img.invalid/p.jpg")]
    assert client.send_medias_msg(media, userid="U1", title="Review")
    blocks = client._client.chat_postMessage.call_args.kwargs["blocks"]
    values = [button["value"] for block in blocks if block["type"] == "actions" for button in block["elements"]]
    assert values[-1] == "2"


@pytest.mark.parametrize("repeat", [800, 3000])
def test_slack_plain_text_preserves_complete_long_body(repeat):
    """
    没有富文本blocks的普通通知也必须完整发送，长文本不能截断。
    """
    client = object.__new__(Slack)
    client._client = Mock()
    body = "完整正文" * repeat
    assert client.send_msg(title="Review", text=body, userid="U1")[0]
    sent = [call.kwargs for call in client._client.chat_postMessage.call_args_list]
    assert "".join(item["text"] for item in sent) == "Review\n" + body
    assert all(len(item["text"]) <= 4000 for item in sent)


def test_notification_config_ids_are_unique():
    """
    不同显示名不能共享同一稳定身份，以免复用另一配置的登录凭据。
    """
    with pytest.raises(ValueError, match="身份|ID|id"):
        _normalize_configs([NotificationConf(id="stable", name="A", type="wechatclawbot"),
                            NotificationConf(id="stable", name="B", type="wechatclawbot")])


@pytest.mark.asyncio
async def test_notification_config_can_repair_legacy_duplicate_ids(monkeypatch):
    """
    拒绝新重复身份的同时，允许管理员把旧重复数据修复为唯一身份。
    """
    service = SimpleNamespace(get=lambda _key: [
        {"id": "legacy-duplicate", "name": "A", "type": "wechatclawbot"},
        {"id": "legacy-duplicate", "name": "B", "type": "wechatclawbot"}], async_set=AsyncMock(return_value=True))
    monkeypatch.setattr(notification_endpoint, "get_configured_system_config", lambda: service)
    reconcile = Mock(return_value={"success": True})
    monkeypatch.setattr(notification_endpoint, "NotificationChain", lambda: SimpleNamespace(manage_channel=reconcile))
    response = await notification_endpoint.save_config([
        NotificationConf(id="unique-A", name="A", type="wechatclawbot"),
        NotificationConf(id="unique-B", name="B", type="wechatclawbot")], None)
    assert response.success
    service.async_set.assert_awaited_once()


def test_discord_invalid_explicit_chat_does_not_fall_back_to_broadcast():
    """
    指定原频道不可访问时拒绝发送，不能泄漏到默认广播频道。
    """
    client = object.__new__(Discord)
    client._broadcast_channel = object()
    client._channel_id = 456
    client._guild_id = None
    client._client = SimpleNamespace(get_channel=lambda _id: None,
        fetch_channel=AsyncMock(side_effect=RuntimeError("channel unavailable")))
    assert asyncio.run(client._resolve_channel(chat_id="789")) is None


def test_remote_restart_preserves_source_in_cache_and_receipt(monkeypatch):
    """
    普通重启的前后通知保持同一配置来源。
    """
    chain = object.__new__(SystemChain)
    messages = []
    saved = {}
    monkeypatch.setattr(chain, "post_message", messages.append)
    monkeypatch.setattr(chain, "save_cache", lambda data, key: saved.update({key: data}))
    monkeypatch.setattr(chain, "load_cache", lambda key: saved.get(key))
    monkeypatch.setattr(chain, "remove_cache", lambda key: saved.pop(key, None))
    monkeypatch.setattr(chain, "backup_plugins", lambda: None)
    monkeypatch.setattr(chain, "_SystemChain__get_version_message", lambda: "review")
    monkeypatch.setattr("app.chain.system.SystemHelper.restart", lambda: None)
    chain.restart(NotificationChannel.Telegram, "U1", source="specific-config")
    assert saved[chain._restart_file]["source"] == "specific-config"
    chain.restart_finish()
    assert [message.source for message in messages] == ["specific-config", "specific-config"]


@pytest.mark.parametrize("prefix", ["搜索", "下载", "订阅", "洗版"])
def test_media_command_removes_only_leading_command(prefix):
    """
    作品名中的同名命令词必须作为检索词保留。
    """
    _, query = resolve_media_action(f"{prefix}：搜索中的订阅与下载洗版")
    assert query == "搜索中的订阅与下载洗版"
