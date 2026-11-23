"""
统一附件协议的插件入口、渠道适配、来源读取和实例隔离回归。
"""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system import attachment
from app.application.messaging.attachment import notification_parts
from app.modules.dingtalk import DingTalkModule
from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.modules.feishu.feishu import Feishu
from app.modules.feishu.module import FeishuModule
from app.modules.qqbot.module import QQBotModule
from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.modules.synologychat import SynologyChatModule
from app.modules.telegram.module import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.modules.vocechat import VoceChatModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.modules.wechatclawbot.wechatclawbot import WechatClawBot
from app.schemas.message import Message, MessageResponse
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import NotificationChannel
from app.sdk.plugin import base as plugin_base
from app.sdk.plugin.base import _PluginBase

_PNG = b"\x89PNG\r\n\x1a\nimage-bytes"
_DATA_URL = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
_IMAGE_URL = "https://images.example/qr.png?token=temporary"
_UPLOAD_CHANNELS = [
    (NotificationChannel.Telegram, TelegramModule, Telegram),
    (NotificationChannel.Feishu, FeishuModule, Feishu),
    (NotificationChannel.Slack, SlackModule, Slack),
    (NotificationChannel.Discord, DiscordModule, Discord),
    (NotificationChannel.Wechat, WechatModule, WeChat),
    (NotificationChannel.Wechat, WechatModule, WeChatBot),
    (NotificationChannel.WechatClawBot, WechatClawBotModule, WechatClawBot),
]
_LINK_CHANNELS = [
    (NotificationChannel.DingTalk, DingTalkModule),
    (NotificationChannel.QQ, QQBotModule),
    (NotificationChannel.SynologyChat, SynologyChatModule),
    (NotificationChannel.VoceChat, VoceChatModule),
]


def _module(monkeypatch: pytest.MonkeyPatch, channel, module_class, client):
    """
    保留真实通知过滤和接收人规则，替换实例目录及渠道发送边界。
    """
    module = module_class()
    module._channel = channel
    conf = SimpleNamespace(id="fixture", identity_namespace="owner", name="source", config={},
                           type=channel.name.lower(), enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {"source": conf})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    monkeypatch.setattr(module, "get_instances", lambda: {"source": client})
    if isinstance(module, FeishuModule):
        monkeypatch.setattr(module, "_resolve_message_target", lambda _: ("member", "chat", "open_id"))
    return module


@pytest.mark.parametrize("channel,module_class,client_class", _UPLOAD_CHANNELS)
@pytest.mark.parametrize("native_image", [True, False])
def test_same_plugin_media_payload_reaches_tracked_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel, module_class, client_class, native_image,
) -> None:
    """
    统一插件参数经过实际模块路由，到达当前实例的持久化附件入口。
    """
    client = client_class.__new__(client_class)
    attachment_path = tmp_path / "source.txt"
    attachment_path.write_text("attachment-content")
    method = {
        Telegram: "send_tracked_image" if native_image else "send_tracked_attachment",
        Slack: "send_tracked_image" if native_image else "send_tracked_file",
        Discord: "send_tracked_notification",
        Feishu: "send_tracked_notification",
        WeChat: "send_tracked_media",
        WeChatBot: "send_msg" if native_image else "send_file",
        WechatClawBot: "send_tracked_notification",
    }[client_class]
    send = Mock(return_value=True)
    monkeypatch.setattr(client, method, send)
    module = _module(monkeypatch, channel, module_class, client)
    monkeypatch.setattr(plugin_base, "settings", SimpleNamespace(MP_DOMAIN=lambda path: f"https://mp.example/{path}"))
    plugin = SimpleNamespace(chain=Mock())
    fields = ({"image": _DATA_URL, "native_image": True} if native_image else
              {"file_path": str(attachment_path), "file_name": "report.txt"})
    _PluginBase.post_message(plugin, channel=channel, userid="member", title="说明", text="正文", **fields)
    message = plugin.chain.post_message.call_args.args[0]
    module.post_message(message)
    send.assert_called_once()
    args, kwargs = send.call_args
    assert all(callable(kwargs[key]) for key in ("receipt", "part_sender", "plan"))
    if client_class is WeChat:
        assert args[0] == (_DATA_URL if native_image else str(attachment_path))
        assert kwargs["media_type"] == ("image" if native_image else "file")
        assert kwargs["userid"] == "member"
    elif client_class is WeChatBot:
        assert all(kwargs[key] == value for key, value in fields.items())
        assert kwargs["userid"] == "member"
    else:
        assert isinstance(args[0], Message)
        assert all(getattr(args[0], key) == value for key, value in fields.items())
        assert args[0].userid == "member"
    assert message.title == "说明" and message.text == "正文"
    assert attachment_path.read_text() == "attachment-content"


@pytest.mark.parametrize("channel,module_class", _LINK_CHANNELS)
def test_url_image_uses_existing_inline_format_and_files_get_explicit_notice(
    monkeypatch: pytest.MonkeyPatch, channel, module_class,
) -> None:
    """
    仅支持图片链接的接入沿用行内图片；不支持文件时明确提示，不能泄露本地绝对路径。
    """
    send = Mock(return_value=True)
    module = _module(monkeypatch, channel, module_class, SimpleNamespace(send_msg=send))
    message = Message(channel=channel, userid="123", title="说明", image=_IMAGE_URL, native_image=True,
                      file_path="/private/secret/report.txt", file_name="附件.txt")

    module.post_message(message)

    assert send.call_count == 2
    image_payload, file_payload = [call.kwargs for call in send.call_args_list]
    assert image_payload["image"] == _IMAGE_URL
    assert "附件未发送" in file_payload["text"]
    assert "附件.txt" in file_payload["text"]
    assert "/private/secret" not in str(send.call_args_list)
    assert "base64" not in str(send.call_args_list)


@pytest.mark.parametrize("channel,module_class", _LINK_CHANNELS)
@pytest.mark.parametrize("image", [_DATA_URL, "/private/secret/image.png"])
def test_link_only_channels_do_not_leak_local_or_encoded_image_source(
    monkeypatch, channel, module_class, image,
) -> None:
    """
    仅支持图片链接的渠道不能把本地路径或 Base64 当作通知正文、封面 URL 泄露给用户。
    """
    sent = Mock(return_value=True)
    module = _module(monkeypatch, channel, module_class, SimpleNamespace(send_msg=sent))

    module.post_message(Message(channel=channel, userid="123", image=image, native_image=True))

    assert sent.call_count == 1
    assert sent.call_args.kwargs["image"] is None
    assert "图片未发送" in sent.call_args.kwargs["text"]
    assert image not in str(sent.call_args_list)


@pytest.mark.parametrize("channel", list(NotificationChannel))
def test_combined_message_is_split_without_mutation_or_lost_routing(channel) -> None:
    """
    每个渠道均从原消息独立适配组合内容，保留用户、来源和隔离路由，只发送一次说明。
    """
    original = Message(channel=channel, source="source", userid="member", title="说明", text="正文",
                       image=_IMAGE_URL, native_image=True, file_path="/private/report.txt", file_name="report.txt")
    original.set_notification_route_scope("user")
    before = original.model_dump()

    parts = notification_parts(original, channel)

    assert len(parts) == 2
    assert all(part.userid == "member" and part.source == "source" for part in parts)
    assert all(part.notification_route_scope == "user" for part in parts)
    assert parts[0].title == "说明"
    assert parts[1].title is None
    assert "正文" not in (parts[1].text or "")
    assert original.model_dump() == before
    assert original.notification_route_scope == "user"


def test_filtered_instance_never_processes_local_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    先按消息来源过滤，再适配附件，不对其他实例执行发送或图片读取。
    """
    client = SimpleNamespace(send_msg=Mock(), send_file=Mock())
    module = _module(monkeypatch, NotificationChannel.Wechat, WechatModule, client)
    message = Message(channel=NotificationChannel.Wechat, source="other", userid="member",
                      image="/private/not-for-this-instance.png", native_image=True)

    module.post_message(message)

    client.send_msg.assert_not_called()
    client.send_file.assert_not_called()


@pytest.mark.parametrize("source_kind", ["http", "data", "local"])
def test_common_image_source_reads_original_bytes_and_releases_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_kind: str,
) -> None:
    """
    URL、Base64 和本地原图使用同一内容，短期文件位于运行配置目录并在发送后清理。
    """
    original = tmp_path / "original.png"
    original.write_bytes(_PNG)
    response = Mock(status_code=200)
    response.iter_content.return_value = [_PNG[:8], _PNG[8:]]
    requests = Mock()
    requests.return_value.get_res.return_value = response
    monkeypatch.setattr(attachment, "RequestUtils", requests)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path / "upload")
    source = {"http": _IMAGE_URL, "data": _DATA_URL, "local": str(original)}[source_kind]

    with attachment.image_upload_file(source) as path:
        assert path is not None and path.read_bytes() == _PNG
        assert path.parent.parent == tmp_path / "upload"
    assert not path.exists()
    assert original.read_bytes() == _PNG
    if source_kind == "http":
        response.close.assert_called_once_with()
    else:
        requests.assert_not_called()


@pytest.mark.parametrize("source", ["data:image/png;base64,!!!", "data:image/png,plain", "file:///private/a.png",
                                   "data:image/gif;base64,R0lGODlhZ2lm", "missing.png"])
def test_bad_image_source_is_rejected_without_temp_artifact(monkeypatch, tmp_path, source) -> None:
    """
    无效来源不会被当成可上传图片，也不产生缓存或临时副本。
    """
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)

    with attachment.image_upload_file(source) as path:
        assert path is None
    assert not list(tmp_path.iterdir())


def test_oversized_http_stream_stops_and_closes_response(monkeypatch) -> None:
    """
    下载超限时不继续消费流，并释放连接。
    """
    monkeypatch.setattr(attachment, "IMAGE_MAX_BYTES", 8)
    response = Mock(status_code=200)
    chunks = iter([b"123456789", b"must-not-read"])
    response.iter_content.return_value = chunks
    requests = Mock()
    requests.return_value.get_res.return_value = response
    monkeypatch.setattr(attachment, "RequestUtils", requests)

    assert attachment.load_image(_IMAGE_URL) is None
    assert next(chunks) == b"must-not-read"
    response.close.assert_called_once_with()


@pytest.mark.parametrize("failure", ["http", "empty", "stream"])
def test_failed_remote_image_is_not_uploaded_and_response_is_released(monkeypatch, failure) -> None:
    """
    HTTP 拒绝、无响应和中途读取失败均返回不可用结果，并释放已取得的响应。
    """
    response = None if failure == "empty" else Mock(status_code=404 if failure == "http" else 200)
    if failure == "stream":
        response.iter_content.side_effect = OSError("stream failed")
    requests = Mock()
    requests.return_value.get_res.return_value = response
    monkeypatch.setattr(attachment, "RequestUtils", requests)

    assert attachment.load_image(_IMAGE_URL) is None
    if response is not None:
        response.close.assert_called_once_with()


def test_image_temporary_copy_is_cleaned_when_sender_raises(monkeypatch, tmp_path) -> None:
    """
    上传边界抛异常时仍清理短期图片，不能把二维码副本留在临时目录。
    """
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    with pytest.raises(RuntimeError):
        with attachment.image_upload_file(_DATA_URL) as path:
            assert path.exists()
            raise RuntimeError("sender failed")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("client_class", [Slack, Discord])
def test_native_image_clients_upload_bytes_and_preserve_caption(monkeypatch, tmp_path, client_class) -> None:
    """
    各客户端从统一图片来源生成上传文件，发送边界读取相同原始字节和说明。
    """
    client = client_class.__new__(client_class)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    captured = {}

    def upload(file_path, **kwargs):
        """
        在客户端同步上传时读取短期文件，随后由来源适配器负责清理。
        """
        path = None if "file_content" in kwargs else Path(file_path)
        captured.update(kwargs, content=kwargs.get("file_content") if path is None else path.read_bytes(), path=path)
        return {"success": True} if client_class is Telegram else (True, {})

    monkeypatch.setattr(client, "send_file", upload)

    result = client.send_image(image=_DATA_URL, title="扫码", text="请扫描", userid="member")

    assert result
    assert captured["content"] == _PNG
    assert captured["title"] == "扫码" and captured["text"] == "请扫描"
    assert captured["userid"] == "member"
    assert captured["path"] is None or not captured["path"].exists()


@pytest.mark.parametrize("client_class", [Slack, Discord])
@pytest.mark.parametrize("caption_succeeds", [False, True])
def test_native_image_buttons_preserve_caption_and_failure_result(
    monkeypatch, tmp_path, client_class, caption_succeeds,
) -> None:
    """
    原图有按钮时只发送一次说明；说明失败后不再上传发送图片，且总会清理短期文件。
    """
    client = client_class.__new__(client_class)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    caption = Mock(return_value=(caption_succeeds, {}))
    upload = Mock(return_value=(True, {}))
    monkeypatch.setattr(client, "send_msg", caption)
    monkeypatch.setattr(client, "send_file", upload)

    result = client.send_image(_DATA_URL, title="扫码", text="说明", userid="member",
                               buttons=[[{"text": "完成", "callback_data": "done"}]])

    assert bool(result and (result[0] if isinstance(result, tuple) else result)) is caption_succeeds
    assert caption.call_count == 1
    assert upload.called is caption_succeeds
    if caption_succeeds:
        assert upload.call_args.kwargs["title"] is None
        assert upload.call_args.kwargs["text"] is None
    assert not list(tmp_path.iterdir())


def test_telegram_native_image_preserves_long_caption_and_raw_bytes(monkeypatch) -> None:
    """
    Telegram 原图沿用长正文发送通路，不能因附件标题上限截断说明。
    """
    client = Telegram.__new__(Telegram)
    client._telegram_token = "token"
    client._telegram_chat_id = "chat"
    monkeypatch.setattr(client, "_determine_target_chat_id", lambda *_args, **_kwargs: "chat")
    monkeypatch.setattr(client, "_is_chat_rate_limited", lambda *_args, **_kwargs: False)
    sent = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(client, "_Telegram__send_request", sent)
    monkeypatch.setattr(client, "_remember_sent_message", Mock())
    text = "说明" * 2000

    assert client.send_image(_DATA_URL, title="图片", text=text, parse_mode="plain")["success"] is True
    assert sent.call_args.kwargs["image"] == _PNG
    assert text in sent.call_args.kwargs["caption"]
    assert Telegram._Telegram__process_image(_PNG) == _PNG


@pytest.mark.parametrize("channel,module_class,client_class", _UPLOAD_CHANNELS)
def test_uploaded_media_capabilities_match_existing_clients(channel, module_class, client_class) -> None:
    """
    已声明可上传原图的渠道也具备文件发送接口，不向插件暴露模式特判。
    """
    assert ChannelCapabilityManager.supports_capability(channel, ChannelCapability.NATIVE_IMAGES)
    assert ChannelCapabilityManager.supports_capability(channel, ChannelCapability.FILE_SENDING)
    assert callable(client_class.send_file)


@pytest.mark.parametrize("module_class", [TelegramModule, FeishuModule, SlackModule, DiscordModule])
@pytest.mark.parametrize("second_succeeds", [False, True])
def test_direct_message_delivers_all_parts_on_same_instance(
    monkeypatch, module_class, second_succeeds,
) -> None:
    """
    直接发送接口也保留组合图片和文件，后续投递锁定首个实例，第二部分失败不得返回完整成功。
    """
    module = module_class()
    receipt = MessageResponse(success=True, source="chosen", message_id="image")
    sender = Mock(side_effect=[receipt, MessageResponse(success=True, source="chosen") if second_succeeds else None])
    monkeypatch.setattr(module, "_send_direct_message_part", sender)
    original = Message(userid="member", image=_IMAGE_URL, native_image=True, file_path="/private/report.txt")

    result = module.send_direct_message(original)

    assert (result is receipt) is second_succeeds
    assert sender.call_count == 2
    assert sender.call_args_list[1].args[0].source == "chosen"
    assert original.source is None


def test_feishu_native_image_uses_image_message_and_preserves_reply(monkeypatch, tmp_path) -> None:
    """
    飞书上传图片后使用独立 image 消息，说明和按钮只发送一次并回复原消息。
    """
    client = Feishu.__new__(Feishu)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    monkeypatch.setattr(client, "_upload_image", lambda _: "image-key")
    caption = Mock(return_value={"success": True})
    reply = Mock(return_value={"success": True, "message_id": "image-message"})
    monkeypatch.setattr(client, "send_notification", caption)
    monkeypatch.setattr(client, "_reply_message", reply)

    result = client.send_image(image=_DATA_URL, title="二维码", text="说明", userid="member",
                               original_message_id="original", buttons=[[{"text": "完成", "callback_data": "done"}]])

    assert result["success"] is True
    assert caption.call_count == 1
    assert caption.call_args.args[0].buttons
    reply.assert_called_once_with("original", "image", {"image_key": "image-key"})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [None, "upload", "caption"])
def test_feishu_native_image_broadcast_reports_failures(monkeypatch, tmp_path, failure) -> None:
    """
    飞书无原消息时保持广播路由，上传或说明失败则不发送图片且返回失败。
    """
    client = Feishu.__new__(Feishu)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    monkeypatch.setattr(client, "_upload_image", lambda _: None if failure == "upload" else "key")
    caption = Mock(return_value={"success": failure != "caption"})
    deliver = Mock(return_value={"success": True})
    monkeypatch.setattr(client, "send_notification", caption)
    monkeypatch.setattr(client, "_send_with_fallback_broadcast", deliver)

    result = client.send_image(_DATA_URL, title="说明" if failure else None, userid="member")

    assert result["success"] is (failure is None)
    assert deliver.called is (failure is None)
    if failure is None:
        assert deliver.call_args.args == ("image", {"image_key": "key"})
        caption.assert_not_called()
    assert not list(tmp_path.iterdir())
