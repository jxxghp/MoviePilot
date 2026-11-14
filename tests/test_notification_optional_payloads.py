"""
验证通知渠道对缺失资源、附件类型及可选实例的兼容处理。
"""

from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
from slack_sdk.models.blocks import ImageElement

from app.domain.context import Context, MediaInfo, TorrentInfo
from app.modules.dingtalk import DingTalkModule
from app.modules.discord import DiscordModule
from app.modules.feishu.module import FeishuModule
from app.modules.qqbot.module import QQBotModule
from app.modules.qqbot.qqbot import QQBot
from app.modules.slack.slack import Slack
from app.modules.synologychat.synologychat import SynologyChat
from app.modules.vocechat import VoceChatModule
from app.modules.vocechat.vocechat import VoceChat
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.schemas.message import Message, MessageResponse
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("client_class", [QQBot, SynologyChat, VoceChat, WeChatBot, Slack])
def test_torrent_notifications_skip_missing_torrent_and_preserve_valid_items(client_class, monkeypatch):
    """
    列表中的缺失资源不阻断有效候选发送，也不会把空上下文渲染为一条种子。
    """
    client = object.__new__(client_class)
    sent = Mock(return_value=True)
    if client_class is QQBot:
        monkeypatch.setattr(client, "send_msg", sent)
    elif client_class is SynologyChat:
        client._webhook_url = "https://chat.test"
        client._token = "test"
        monkeypatch.setattr(client, "_SynologyChat__send_request", sent)
    elif client_class is VoceChat:
        client._client = Mock()
        monkeypatch.setattr(client, "_VoceChat__send_request", sent)
    elif client_class is WeChatBot:
        monkeypatch.setattr(client, "_send_markdown", sent)
    else:
        client._client = SimpleNamespace(chat_postMessage=sent)
    valid = Context(torrent_info=TorrentInfo(title="Demo.2026.1080p", site_name="ExampleSite", size=1024, seeders=3))
    assert client.send_torrents_msg([Context(), valid], userid="1", title="候选") is True
    assert sent.call_count == 1
    assert "ExampleSite" in str(sent.call_args)
    assert str(sent.call_args).count("ExampleSite") == 1


def test_dingtalk_missing_torrent_does_not_hide_later_candidate(monkeypatch):
    """
    钉钉 Markdown 列表跳过空资源后继续展示可点击的有效候选。
    """
    module = object.__new__(DingTalkModule)
    sent = Mock()
    monkeypatch.setattr(module, "_post_list_message", sent)
    message = Message(title="候选")
    module.post_torrents_message(message, [Context(), Context(torrent_info=TorrentInfo(
        title="Demo", page_url="https://site.test/details/1",
    ))])
    sent.assert_called_once_with(message, "2. [Demo](https://site.test/details/1)")


@pytest.mark.parametrize(("module_class", "scheme", "method", "module_path"), [
    (DiscordModule, "discord", "download_discord_file_bytes", "app.modules.discord"),
    (QQBotModule, "qq", "download_qq_file_bytes", "app.modules.qqbot.module"),
])
@pytest.mark.parametrize("payload", [b"attachment", "invalid text", None])
def test_attachment_downloads_require_bytes(module_class, scheme, method, module_path, payload, monkeypatch):
    """
    附件传输仅返回原始字节，错误文本或空响应不会进入语音与文件处理流程。
    """
    module = object.__new__(module_class)
    monkeypatch.setattr(module, "get_config", Mock(return_value=SimpleNamespace(name="source")))
    request = SimpleNamespace(get_res=Mock(return_value=SimpleNamespace(content=payload)))
    monkeypatch.setattr(f"{module_path}.RequestUtils", Mock(return_value=request))
    result = getattr(module, method)(f"{scheme}://file/https://cdn.test/file", "source")
    assert result == (payload if isinstance(payload, bytes) else None)
    request.get_res.assert_called_once_with("https://cdn.test/file")


@pytest.mark.parametrize("reference", ["wxwork://media_id/image", "wxbot://image/image"])
@pytest.mark.parametrize("payload", ["data:image/png;base64,c2FmZQ==", b"invalid"])
def test_wechat_images_only_expose_data_url_text(reference, payload, monkeypatch):
    """
    应用与机器人图片下载结果必须是文本地址，字节结果不能作为 data URL 外泄。
    """
    module = object.__new__(WechatModule)
    client = SimpleNamespace(download_media_to_data_url=Mock(return_value=payload), download_image_to_data_url=Mock(return_value=payload))
    monkeypatch.setattr(module, "get_config", Mock(return_value=SimpleNamespace(name="source")))
    monkeypatch.setattr(module, "get_instance", Mock(return_value=client))
    assert module.download_wechat_image_to_data_url(reference, "source") == (payload if isinstance(payload, str) else None)
    assert module._commands_enabled(None) is False


@pytest.mark.parametrize("module_class", [VoceChatModule, WechatClawBotModule])
def test_notification_without_active_client_does_not_send(module_class, monkeypatch):
    """
    配置存在而客户端尚未激活时，列表通知停止发送，不访问可选实例属性。
    """
    module = object.__new__(module_class)
    monkeypatch.setattr(module, "get_configs", Mock(return_value={"source": SimpleNamespace(name="source")}))
    monkeypatch.setattr(module, "check_message", Mock(return_value=True))
    instance = Mock(return_value=None)
    monkeypatch.setattr(module, "get_instance", instance)
    module.post_medias_message(Message(title="媒体"), [MediaInfo(title="Demo")])
    assert instance.call_count == 1


def test_feishu_invalid_stream_state_does_not_finalize_card(monkeypatch):
    """
    非字典流式卡片状态不能被当作可关闭的消息，避免发送错误的卡片 ID。
    """
    module = object.__new__(FeishuModule)
    module._channel = NotificationChannel.Feishu
    client = Mock()
    monkeypatch.setattr(module, "get_instance", client)
    response = MessageResponse(channel=NotificationChannel.Feishu, metadata={"feishu_streaming": ["invalid"]})
    assert module.finalize_message(response) is False
    client.assert_not_called()


@pytest.mark.parametrize(("manifest", "expected"), [("{\"features\": {}}", {"features": {}}), ([], None)])
def test_slack_manifest_export_requires_object(manifest, expected):
    """
    Slack 清单支持 JSON 文本与对象，错误集合类型不会进入命令更新流程。
    """
    client = object.__new__(Slack)
    client._app_id = "app"
    client._manifest_client = SimpleNamespace(apps_manifest_export=Mock(return_value={"ok": True, "manifest": manifest}))
    assert client._export_manifest() == expected


def _notification_client(client_class):
    """
    构造只记录最终发送内容的 Slack、VoceChat 与 Synology Chat 客户端。
    """
    sent = Mock(return_value=True)
    client = object.__new__(client_class)
    if client_class is Slack:
        client._client = SimpleNamespace(chat_postMessage=sent)
        client._channel = "C1"
        client._service_name = "slack"
    elif client_class is VoceChat:
        client._client = Mock()
        client._channel_id = 1
        client._VoceChat__send_request = sent
    else:
        client._webhook_url = "https://chat.test"
        client._token = "test"
        client._SynologyChat__send_request = sent
    return client, sent


@pytest.mark.parametrize("client_class", [Slack, VoceChat, SynologyChat])
@pytest.mark.parametrize("title", [None, ""])
@pytest.mark.parametrize("kwargs", [
    {"text": "仅正文"},
    {"text": "仅正文", "image": "https://img.test/a.png"},
    {"text": "仅正文", "link": "https://site.test/a"},
])
def test_text_only_notifications_do_not_render_none_title(client_class, title, kwargs):
    """
    只有正文的通知不能把缺失标题渲染成字面量 None。
    """
    client, sent = _notification_client(client_class)
    result = client.send_msg(title=title, userid="1", **kwargs)
    assert (result[0] if client_class is Slack else result) is True
    assert sent.call_count == 1
    rendered = unquote(str(sent.call_args))
    assert "None" not in rendered
    assert "仅正文" in rendered


@pytest.mark.parametrize("client_class,caption", [
    (Slack, "通知标题\n正文"),
    (VoceChat, "**通知标题**\n正文"),
    (SynologyChat, "*通知标题*\n正文"),
])
def test_notifications_preserve_existing_title_formatting(client_class, caption):
    """
    提供标题时保留各渠道既有的标题格式和正文。
    """
    client, sent = _notification_client(client_class)
    client.send_msg(title="通知标题", text="正文", userid="1")
    sent.assert_called_once()
    if client_class is Slack:
        assert sent.call_args.kwargs["text"] == caption
    elif client_class is VoceChat:
        assert sent.call_args.kwargs["caption"] == caption
    else:
        assert unquote(sent.call_args.args[0]["text"]) == caption


@pytest.mark.parametrize("title", [None, ""])
def test_slack_title_free_buttons_preserve_body_and_action(title):
    """
    无标题的按钮通知保留正文与回调动作，不生成缺失标题。
    """
    client, sent = _notification_client(Slack)
    buttons = [[{"text": "继续", "callback_data": "continue"}]]
    assert client.send_msg(title=title, text="正文", userid="1", buttons=buttons)[0] is True
    payload = sent.call_args.kwargs
    assert payload["text"] == "正文"
    assert payload["blocks"][0]["text"]["text"] == "正文"
    assert payload["blocks"][1]["elements"][0]["value"] == "continue"


@pytest.mark.parametrize("title", [None, ""])
@pytest.mark.parametrize("length", [2000, 2001])
def test_slack_title_free_image_alt_text_respects_sdk_limit(title, length):
    """
    无标题图文通知的图片摘要遵守 SDK 长度限制，正文仍完整保留。
    """
    client, sent = _notification_client(Slack)
    text = "文" * length
    assert client.send_msg(title=title, text=text, image="https://img.test/a.png", userid="1")[0] is True
    block = sent.call_args.kwargs["blocks"][0]
    assert block["text"]["text"] == text
    ImageElement(**block["accessory"]).validate_json()
    assert block["accessory"]["alt_text"] == text[:2000]
