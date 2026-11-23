"""
企业微信机器人长连接媒体上传与统一图片、文件发送协议回归。
"""

import base64
import threading
from unittest.mock import Mock

import pytest

from app.modules.wechat.wechatbot import WeChatBot


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> tuple[WeChatBot, list[dict]]:
    """
    用同步服务器回执替身驱动真实认证等待与确认关联，不创建连接或后台线程。
    """
    client = WeChatBot.__new__(WeChatBot)
    client._authenticated = threading.Event()
    client._authenticated.set()
    client._acks_lock = threading.Lock()
    client._send_lock = threading.Lock()
    client._pending_acks = {}
    client._known_targets = set()
    client._default_chat_id = "user:member"
    client._config_name = "test-bot"
    recorded = []

    def send(payload):
        """
        模拟上传初始化、分片和完成确认，分片确认允许没有 body。
        """
        recorded.append(payload)
        body = {"aibot_upload_media_init": {"upload_id": "upload"},
                "aibot_upload_media_finish": {"media_id": "media"}}.get(payload["cmd"])
        ack = {"errcode": 0, **({"body": body} if body else {})}
        client._resolve_ack(payload["headers"]["req_id"], ack)

    monkeypatch.setattr(client, "_send_raw", send)
    return client, recorded


@pytest.mark.parametrize("userid,chat_type,target", [("user:member", 1, "member"), ("group:chat", 2, "chat")])
def test_bot_file_upload_waits_for_each_ack_and_preserves_target(bot, tmp_path, userid, chat_type, target) -> None:
    """
    上传按 init、所有分片、finish 排序，随后独立发送说明与文件，并保留群聊或私聊目标。
    """
    client, frames = bot
    path = tmp_path / "attachment.txt"
    content = b"a" * (512 * 1024 + 17)
    path.write_bytes(content)

    assert client.send_file(str(path), file_name="report.txt", title="附件", text="说明", userid=userid) is True

    assert [frame["cmd"] for frame in frames] == [
        "aibot_upload_media_init", "aibot_upload_media_chunk", "aibot_upload_media_chunk",
        "aibot_upload_media_finish", "aibot_send_msg", "aibot_send_msg",
    ]
    assert frames[0]["body"]["filename"] == "report.txt"
    assert frames[0]["body"]["total_chunks"] == 2
    chunks = frames[1:3]
    assert [frame["body"]["chunk_index"] for frame in chunks] == [1, 2]
    assert b"".join(base64.b64decode(frame["body"]["base64_data"]) for frame in chunks) == content
    assert "说明" in frames[-2]["body"]["markdown"]["content"]
    assert frames[-1]["body"] == {"chatid": target, "chat_type": chat_type, "msgtype": "file",
                                    "file": {"media_id": "media"}}
    assert path.read_bytes() == content
    assert client._pending_acks == {}


def test_bot_native_image_uploads_original_png_without_default_link(bot) -> None:
    """
    机器人模式同样接受 native_image，无需插件判断应用模式，且不把默认详情链接嵌入图片。
    """
    client, frames = bot
    content = b"\x89PNG\r\n\x1a\noriginal-png"
    image = f"data:image/png;base64,{base64.b64encode(content).decode()}"

    assert client.send_msg(title="扫码", text="说明", image=image, native_image=True,
                            link="https://mp.example/#/plugins?id=test") is True

    assert frames[0]["body"]["type"] == "image"
    assert base64.b64decode(frames[1]["body"]["base64_data"]) == content
    assert frames[-1]["body"]["image"] == {"media_id": "media"}
    assert "#/plugins" not in str(frames[-2:])


def test_failed_chunk_is_retried_then_stops_without_sending_later_parts(bot, monkeypatch) -> None:
    """
    失败分片耗尽两次重试后停止，不能发送下一片、完成命令或消息。
    """
    client, _ = bot
    command = Mock(side_effect=[{"upload_id": "upload"}, None, None, None])
    monkeypatch.setattr(client, "_media_command", command)

    assert client._upload_media(b"a" * (512 * 1024 + 1), "file", "report.txt") is None
    assert [call.args[0] for call in command.call_args_list] == [
        "aibot_upload_media_init", "aibot_upload_media_chunk", "aibot_upload_media_chunk", "aibot_upload_media_chunk",
    ]
    assert all(call.args[1]["chunk_index"] == 1 for call in command.call_args_list[1:])


@pytest.mark.parametrize("initialized,completed", [({}, {}), ({"upload_id": "upload"}, {}),
                                                  ({"upload_id": "upload"}, {"media_id": 12})])
def test_missing_upload_identifiers_cannot_become_send_success(bot, monkeypatch, initialized, completed) -> None:
    """
    初始化或完成回执缺少合法标识时不发送媒体消息。
    """
    client, frames = bot

    def command(name, _body):
        """
        仅提供指定阶段的外部响应，验证必要标识的类型和空值边界。
        """
        return initialized if name.endswith("init") else completed if name.endswith("finish") else {}

    monkeypatch.setattr(client, "_media_command", command)
    assert client._send_media(b"content", "file", "x.txt", None, None, None) is False
    assert not frames


@pytest.mark.parametrize("size", [0, 5, 20 * 1024 * 1024 + 1])
def test_bot_rejects_invalid_file_size_before_protocol_upload(bot, tmp_path, size) -> None:
    """
    机器人与自建应用使用相同的文件大小边界，拒绝后保留源文件。
    """
    client, frames = bot
    path = tmp_path / "invalid.txt"
    with path.open("wb") as file:
        file.truncate(size)

    assert client.send_file(str(path)) is False
    assert not frames
    assert path.stat().st_size == size


def test_bot_does_not_upload_without_recipient(bot, monkeypatch) -> None:
    """
    缺少默认或已知接收人时不上传素材，更不能回退为全员通知。
    """
    client, frames = bot
    client._default_chat_id = None
    upload = Mock()
    monkeypatch.setattr(client, "_upload_media", upload)

    assert client._send_media(b"content", "file", "x.txt", None, None, None) is False
    upload.assert_not_called()
    assert not frames


def test_bot_does_not_send_media_after_caption_rejection(bot, monkeypatch) -> None:
    """
    附带说明被服务器拒绝时不得继续发送文件或返回完整成功。
    """
    client, frames = bot
    monkeypatch.setattr(client, "_send_markdown", lambda *_args: False)

    assert client._send_media(b"content", "file", "x.txt", "标题", "说明", None) is False
    assert frames[-1]["cmd"] == "aibot_upload_media_finish"
