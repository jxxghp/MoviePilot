"""
验证企业微信普通文件上传、通知路由和 Agent 按实例模式开放附件能力。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.tools.impl import send_local_file as file_tool
from app.agent.tools.impl.send_local_file import SendLocalFileTool
from app.modules.wechat import WechatModule
from app.modules.wechat import wechat as wechat_client
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas.message import Message
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import NotificationChannel
from app.sdk.plugin import base as plugin_base
from app.sdk.plugin.base import _PluginBase


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """
    隔离上传和消息发送接口，默认返回成功且不发起真实请求。
    """
    request_utils = Mock()
    transport = request_utils.return_value
    transport.request.return_value.json.return_value = {"errcode": 0, "media_id": "file-media"}
    transport.post.return_value.status_code = 200
    transport.post.return_value.json.return_value = {"errcode": 0}
    monkeypatch.setattr(wechat_client, "RequestUtils", request_utils)
    return transport


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> WeChat:
    """
    创建固定令牌的应用客户端，跳过构造时的网络访问。
    """
    result = WeChat.__new__(WeChat)
    result._appid = "1000002"
    result._send_msg_url = "https://wecom.example/message/send?access_token={access_token}"
    result._upload_media_url = "https://wecom.example/media/upload?access_token={access_token}&type={media_type}"
    monkeypatch.setattr(result, "_WeChat__get_access_token", Mock(return_value="access-token"))
    return result


@pytest.fixture
def attachment(tmp_path: Path) -> Path:
    """
    创建需要在发送成败后完整保留的字幕文件。
    """
    source = tmp_path / "电影字幕.srt"
    source.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nsubtitle\n")
    return source


def _payloads(http: Mock) -> list[dict]:
    """
    按请求顺序解码实际发送的 JSON 字节。
    """
    return [json.loads(call.kwargs["data"]) for call in http.post.call_args_list]


@pytest.mark.parametrize("userid", [None, "member-1"])
@pytest.mark.parametrize("file_name", [None, "展示名称.srt", "../字幕.srt"])
def test_upload_and_send_file_preserves_source_and_display_name(
    client: WeChat, http: Mock, attachment: Path, userid: str | None, file_name: str | None,
) -> None:
    """
    上传原始文件内容及展示名称，并分别发送说明文字与文件，不删除源文件。
    """
    content = attachment.read_bytes()

    assert client.send_file(str(attachment), file_name=file_name, title="字幕", text="请下载查看", userid=userid) is True

    upload = http.request.call_args.kwargs
    assert upload["url"] == "https://wecom.example/media/upload?access_token=access-token&type=file"
    assert upload["files"] == {
        "media": (Path(file_name or attachment.name).name, content, "application/octet-stream"),
    }
    sent = _payloads(http)
    assert [payload["msgtype"] for payload in sent] == ["text", "file"]
    assert "字幕" in sent[0]["text"]["content"]
    assert "请下载查看" in sent[0]["text"]["content"]
    assert sent[1] == {
        "touser": userid or "@all", "msgtype": "file", "agentid": "1000002",
        "file": {"media_id": "file-media"},
    }
    assert attachment.read_bytes() == content


def test_file_without_caption_sends_single_message(client: WeChat, http: Mock, attachment: Path) -> None:
    """
    纯附件通知只发送文件，不产生空文本。
    """
    assert client.send_file(str(attachment)) is True
    assert [payload["msgtype"] for payload in _payloads(http)] == ["file"]


@pytest.mark.parametrize("size, accepted", [(0, False), (5, False), (6, True), (16, True), (17, False)])
def test_file_size_boundary_is_enforced_before_upload(
    client: WeChat, http: Mock, attachment: Path, monkeypatch: pytest.MonkeyPatch, size: int, accepted: bool,
) -> None:
    """
    小于等于 5 字节和超过上限的文件均在上传前拒绝，大小边界内允许发送。
    """
    monkeypatch.setattr(wechat_client, "_FILE_MAX_BYTES", 16)
    attachment.write_bytes(b"x" * size)

    assert client.send_file(str(attachment)) is accepted
    assert http.request.called is accepted
    assert http.post.called is accepted
    assert attachment.read_bytes() == b"x" * size


@pytest.mark.parametrize("source", ["missing.srt", "."])
def test_unreadable_file_does_not_upload(
    client: WeChat, http: Mock, tmp_path: Path, source: str,
) -> None:
    """
    文件缺失或传入目录时返回失败，不继续上传或发消息。
    """
    assert client.send_file(str(tmp_path / source)) is False
    http.request.assert_not_called()
    http.post.assert_not_called()


@pytest.mark.parametrize("payload", [{"errcode": 40005}, {"errcode": 0}, {"errcode": 0, "media_id": 1}])
def test_failed_upload_preserves_file_without_sending_caption(
    client: WeChat, http: Mock, attachment: Path, payload: dict,
) -> None:
    """
    上传失败或缺少合法素材 ID 时不发说明文字，且保留源文件。
    """
    content = attachment.read_bytes()
    http.request.return_value.json.return_value = payload

    assert client.send_file(str(attachment), title="附件") is False
    http.post.assert_not_called()
    assert attachment.read_bytes() == content


@pytest.mark.parametrize("title", [None, "附件"])
def test_rejected_message_returns_failure_and_preserves_file(
    client: WeChat, http: Mock, attachment: Path, title: str | None,
) -> None:
    """
    文件消息或说明文字被拒绝时返回失败，不能删除源文件或当作成功。
    """
    http.post.return_value.json.return_value = {"errcode": 40003}

    assert client.send_file(str(attachment), title=title) is False
    assert len(_payloads(http)) == 1
    assert attachment.exists()


def test_missing_token_does_not_read_file(
    client: WeChat, http: Mock, attachment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    无访问令牌时保留既有空返回值，不读取文件或上传。
    """
    monkeypatch.setattr(client, "_WeChat__get_access_token", Mock(return_value=None))
    read = Mock(side_effect=AssertionError("不应读取文件"))
    monkeypatch.setattr(Path, "open", read)

    assert client.send_file(str(attachment)) is None
    read.assert_not_called()
    http.request.assert_not_called()


def _module(monkeypatch: pytest.MonkeyPatch, client: WeChat | WeChatBot) -> WechatModule:
    """
    装配单实例模块，保留真实通知过滤和接收人解析。
    """
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    conf = SimpleNamespace(name="wechat-test", config={})
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    return module


def test_plugin_file_fields_reach_application_client(
    client: WeChat, http: Mock, attachment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    插件通用附件字段及用户目标经消息序列化后到达文件发送接口，默认链接不会覆盖附件。
    """
    monkeypatch.setattr(plugin_base, "settings", SimpleNamespace(MP_DOMAIN=lambda path: f"https://mp.example/{path}"))
    plugin = SimpleNamespace(chain=Mock())
    _PluginBase.post_message(
        plugin, channel=NotificationChannel.Wechat, userid="member-1", title="附件",
        file_path=str(attachment), file_name="download.srt",
    )
    message = plugin.chain.post_message.call_args.args[0]
    module = _module(monkeypatch, client)

    module.post_message(Message.model_validate(message.model_dump()))

    sent = _payloads(http)
    assert [payload["msgtype"] for payload in sent] == ["text", "file"]
    assert all(payload["touser"] == "member-1" for payload in sent)
    assert "#/plugins" not in sent[0]["text"]["content"]
    assert http.request.call_args.kwargs["files"]["media"][0] == "download.srt"


def test_file_notification_keeps_instance_and_channel_filters(
    client: WeChat, http: Mock, attachment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    原有来源与渠道过滤仍阻止文件被投递给错误实例。
    """
    module = _module(monkeypatch, client)
    module.post_message(Message(file_path=str(attachment), userid="member-1", source="other"))
    module.post_message(Message(file_path=str(attachment), userid="member-1", channel=NotificationChannel.Telegram))

    http.request.assert_not_called()
    http.post.assert_not_called()


def test_bot_does_not_silently_replace_file_with_text(
    attachment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    机器人模式拒绝文件发送，不将未送出的附件伪装为普通文本通知。
    """
    bot = WeChatBot.__new__(WeChatBot)
    send = Mock()
    monkeypatch.setattr(bot, "send_msg", send)

    _module(monkeypatch, bot).post_message(Message(file_path=str(attachment), title="附件", userid="member-1"))

    send.assert_not_called()


@pytest.mark.parametrize("mode, source_exists, accepted", [
    ("app", True, True), (None, True, True), ("bot", True, False), ("app", False, False),
])
def test_agent_file_sending_checks_actual_wechat_mode(
    attachment: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None, source_exists: bool, accepted: bool,
) -> None:
    """
    Agent 只向当前存在的自建应用实例发送附件，默认模式兼容旧配置。
    """
    config = {"WECHAT_MODE": mode} if mode else {}
    conf = SimpleNamespace(name="wechat-test", config=config)
    monkeypatch.setattr(file_tool, "get_notification_configs", lambda: [conf] if source_exists else [])
    send = AsyncMock()
    monkeypatch.setattr(SendLocalFileTool, "send_message", send)
    tool = SendLocalFileTool(session_id="file-test", user_id="member-1")
    tool.set_message_attr(channel=NotificationChannel.Wechat.value, source="wechat-test", username="member")

    result = asyncio.run(tool.run(str(attachment), file_name="download.srt"))

    assert ChannelCapabilityManager.supports_capability(NotificationChannel.Wechat, ChannelCapability.FILE_SENDING)
    assert bool(send.await_count) is accepted
    if accepted:
        assert result == "本地附件已发送"
        assert send.await_args.args[0].file_path == str(attachment)
        assert send.await_args.args[0].file_name == "download.srt"
        assert send.await_args.args[0].userid == "member-1"
    else:
        assert "仅自建应用" in result


def test_other_file_channels_do_not_require_wechat_configuration(
    attachment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    现有附件渠道不读取企业微信实例配置，保持原有文件发送能力。
    """
    config_lookup = Mock(side_effect=AssertionError("其他渠道不应查询企业微信配置"))
    monkeypatch.setattr(file_tool, "get_notification_configs", config_lookup)
    send = AsyncMock()
    monkeypatch.setattr(SendLocalFileTool, "send_message", send)
    tool = SendLocalFileTool(session_id="file-test", user_id="member-1")
    tool.set_message_attr(channel=NotificationChannel.Telegram.value, source="telegram-test", username="member")

    assert asyncio.run(tool.run(str(attachment))) == "本地附件已发送"
    config_lookup.assert_not_called()
    send.assert_awaited_once()
