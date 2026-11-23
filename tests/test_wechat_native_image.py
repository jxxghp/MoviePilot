"""
验证企业微信显式原生图片通知的 SDK 透传、素材上传及默认图文兼容性。
"""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.wechat import WechatModule
from app.modules.wechat import wechat as wechat_client
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel
from app.sdk.plugin import base as plugin_base
from app.sdk.plugin.base import _PluginBase

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jYH0AAAAASUVORK5CYII="
)
_PNG_DATA_URL = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
_IMAGE_URL = "https://images.example/login.png?token=private"
_SEND_URL = "https://wecom.example/cgi-bin/message/send?access_token={access_token}"
_UPLOAD_URL = "https://wecom.example/cgi-bin/media/upload?access_token={access_token}&type={media_type}"


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """
    替换图片下载、临时素材上传和消息发送，禁止真实网络请求。
    """
    request_utils = Mock()
    transport = request_utils.return_value
    transport.get_res.return_value.status_code = 200
    transport.get_res.return_value.iter_content.return_value = [_PNG[:8], _PNG[8:]]
    transport.request.return_value.json.return_value = {"errcode": 0, "media_id": "image-media"}
    transport.post.return_value.status_code = 200
    transport.post.return_value.json.return_value = {"errcode": 0}
    monkeypatch.setattr(wechat_client, "RequestUtils", request_utils)
    return transport


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> WeChat:
    """
    提供无初始化网络请求、令牌固定且不影响其他实例的自建应用客户端。
    """
    result = WeChat.__new__(WeChat)
    result._appid = "1000002"
    result._send_msg_url = _SEND_URL
    result._upload_media_url = _UPLOAD_URL
    monkeypatch.setattr(result, "_WeChat__get_access_token", Mock(return_value="access-token"))
    return result


def _payloads(http: Mock) -> list[dict]:
    """
    解码消息发送请求，按发送顺序保留载荷。
    """
    return [json.loads(call.kwargs["data"]) for call in http.post.call_args_list]


@pytest.mark.parametrize("image", [_IMAGE_URL, _PNG_DATA_URL])
@pytest.mark.parametrize("userid", [None, "member-1"])
def test_native_image_uploads_original_bytes_and_sends_caption_separately(
    client: WeChat, http: Mock, image: str, userid: str | None,
) -> None:
    """
    图片先上传原始字节，说明文字和图片分别发送且不夹带插件详情链接。
    """
    assert client.send_msg(
        title="扫码登录", text="请在过期前扫描二维码", image=image, userid=userid,
        link="https://moviepilot.example/#/plugins?id=Login", native_image=True,
    ) is True

    upload = http.request.call_args.kwargs
    assert upload["url"] == "https://wecom.example/cgi-bin/media/upload?access_token=access-token&type=image"
    assert upload["headers"] == {"Accept": "application/json"}
    assert upload["files"] == {"media": ("image.png", _PNG, "image/png")}
    sent = _payloads(http)
    assert [payload["msgtype"] for payload in sent] == ["text", "image"]
    assert "扫码登录" in sent[0]["text"]["content"]
    assert "请在过期前扫描二维码" in sent[0]["text"]["content"]
    assert "moviepilot.example" not in sent[0]["text"]["content"]
    assert sent[1] == {
        "touser": userid or "@all", "msgtype": "image", "agentid": "1000002",
        "image": {"media_id": "image-media"},
    }
    if image == _IMAGE_URL:
        http.get_res.assert_called_once_with(_IMAGE_URL, stream=True)
        http.get_res.return_value.close.assert_called_once_with()
    else:
        http.get_res.assert_not_called()


def test_native_image_without_caption_sends_only_image(client: WeChat, http: Mock) -> None:
    """
    纯图片通知只发送一条原生图片，不额外发送空文本。
    """
    assert client.send_msg(title=None, image=_PNG_DATA_URL, native_image=True) is True
    assert [payload["msgtype"] for payload in _payloads(http)] == ["image"]


def test_native_jpeg_uses_jpeg_filename_and_mime_type(client: WeChat, http: Mock) -> None:
    """
    JPG 素材保留原始内容，并使用匹配的文件名与 MIME 类型。
    """
    image = b"\xff\xd8\xfforiginal-jpeg"
    data_url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"

    assert client.send_msg(title=None, image=data_url, native_image=True) is True
    assert http.request.call_args.kwargs["files"] == {"media": ("image.jpg", image, "image/jpeg")}


@pytest.mark.parametrize("native_image", [False, True])
def test_missing_image_keeps_text_delivery(client: WeChat, http: Mock, native_image: bool) -> None:
    """
    没有图片时保持原有文本投递，不因原生图片开关请求上传。
    """
    assert client.send_msg(title="标题", text="正文", native_image=native_image) is True
    assert [payload["msgtype"] for payload in _payloads(http)] == ["text"]
    http.get_res.assert_not_called()
    http.request.assert_not_called()


def test_default_image_keeps_news_card(client: WeChat, http: Mock) -> None:
    """
    未显式启用时仍发送包含封面和跳转地址的图文卡片，不下载图片。
    """
    assert client.send_msg(title="新增媒体", text="已入库", image=_IMAGE_URL, link="https://detail.example") is True
    article = _payloads(http)[0]["news"]["articles"][0]
    assert article == {
        "title": "新增媒体", "description": "已入库", "picurl": _IMAGE_URL, "url": "https://detail.example",
    }
    http.get_res.assert_not_called()
    http.request.assert_not_called()


@pytest.mark.parametrize("image", [
    "file:///private/image.png", "/local/image.png", "data:image/png;base64,!!!",
    "data:image/png,unencoded", "data:image/png;base64,YWJjZA==",
    "data:image/gif;base64,R0lGODlhZ2lm", "data:image/png;base64,aHRtbCBub3QgaW1hZ2U=",
])
def test_invalid_native_images_do_not_upload_or_send(client: WeChat, http: Mock, image: str) -> None:
    """
    拒绝本地路径、无效 Base64、过短内容以及不支持的格式，不回退为裁切图文。
    """
    assert client.send_msg(title="扫码登录", image=image, native_image=True) is False
    http.get_res.assert_not_called()
    http.request.assert_not_called()
    http.post.assert_not_called()


def test_oversized_data_url_is_rejected_before_decode(
    client: WeChat, http: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Base64 数据超过原图预算时直接拒绝，不解码或上传。
    """
    monkeypatch.setattr(wechat_client, "_IMAGE_MAX_BYTES", 6)
    decode = Mock(side_effect=AssertionError("不应解码超限图片"))
    monkeypatch.setattr(wechat_client.base64, "b64decode", decode)

    assert client.send_msg(title=None, image=_PNG_DATA_URL, native_image=True) is False
    decode.assert_not_called()
    http.request.assert_not_called()


@pytest.mark.parametrize("status_code, chunks", [
    (404, [_PNG]), (200, [b"<html>error</html>"]), (200, [b""]), (200, [_PNG, b"too-large"]),
])
def test_failed_or_invalid_download_closes_response_and_sends_nothing(
    client: WeChat, http: Mock, monkeypatch: pytest.MonkeyPatch, status_code: int, chunks: list[bytes],
) -> None:
    """
    HTTP 错误、空响应、错误格式和超限下载均关闭连接且不发送消息。
    """
    monkeypatch.setattr(wechat_client, "_IMAGE_MAX_BYTES", len(_PNG))
    response = http.get_res.return_value
    response.status_code = status_code
    response.iter_content.return_value = chunks

    assert client.send_msg(title="扫码登录", image=_IMAGE_URL, native_image=True) is False
    response.close.assert_called_once_with()
    http.request.assert_not_called()
    http.post.assert_not_called()


def test_failed_stream_closes_response(client: WeChat, http: Mock) -> None:
    """
    读取中途抛出异常时也释放 HTTP 响应，不发送不完整图片。
    """
    http.get_res.return_value.iter_content.side_effect = OSError("读取失败")

    assert client.send_msg(title=None, image=_IMAGE_URL, native_image=True) is False
    http.get_res.return_value.close.assert_called_once_with()
    http.request.assert_not_called()
    http.post.assert_not_called()


@pytest.mark.parametrize("payload", [{"errcode": 40005}, {"errcode": 0}, {"errcode": 0, "media_id": 3}])
def test_failed_upload_does_not_send_caption_or_news(client: WeChat, http: Mock, payload: dict) -> None:
    """
    上传失败或缺少有效素材 ID 时停止发送，避免产生空通知或回退图文。
    """
    http.request.return_value.json.return_value = payload

    assert client.send_msg(title="扫码登录", image=_PNG_DATA_URL, native_image=True) is False
    http.post.assert_not_called()


@pytest.mark.parametrize("response_target", ["request", "get_res"])
def test_missing_http_response_fails_without_sending(
    client: WeChat, http: Mock, response_target: str,
) -> None:
    """
    下载或上传无响应时返回失败，不继续投递通知。
    """
    getattr(http, response_target).return_value = None

    assert client.send_msg(title="扫码登录", image=_IMAGE_URL, native_image=True) is False
    http.post.assert_not_called()


def test_invalid_upload_json_does_not_send(client: WeChat, http: Mock) -> None:
    """
    素材接口返回无法解析的响应时停止发送，不暴露为发送成功。
    """
    http.request.return_value.json.side_effect = ValueError("无效 JSON")

    assert client.send_msg(title="扫码登录", image=_PNG_DATA_URL, native_image=True) is False
    http.post.assert_not_called()


def test_missing_access_token_does_not_load_image(
    client: WeChat, http: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    没有访问令牌时沿用原返回值，不下载或上传图片。
    """
    monkeypatch.setattr(client, "_WeChat__get_access_token", Mock(return_value=None))

    assert client.send_msg(title=None, image=_IMAGE_URL, native_image=True) is None
    http.get_res.assert_not_called()
    http.request.assert_not_called()
    http.post.assert_not_called()


@pytest.mark.parametrize("title", [None, "扫码登录"])
def test_send_error_is_reported(client: WeChat, http: Mock, title: str | None) -> None:
    """
    企业微信拒绝说明文字或图片时返回失败，不声称发送成功。
    """
    http.post.return_value.json.return_value = {"errcode": 40003}

    assert client.send_msg(title=title, image=_PNG_DATA_URL, native_image=True) is False
    assert len(_payloads(http)) == 1


def test_voice_upload_still_reads_local_file(client: WeChat, http: Mock, tmp_path: Path) -> None:
    """
    复用上传方法后，现有语音路径仍读取本地文件并声明 AMR 类型。
    """
    voice_path = tmp_path / "voice.amr"
    voice_path.write_bytes(b"#!AMR\nvoice")

    assert client._upload_temp_media(voice_path) == "image-media"
    upload = http.request.call_args.kwargs
    assert upload["url"].endswith("&type=voice")
    assert upload["files"] == {"media": ("voice.amr", b"#!AMR\nvoice", "voice/amr")}
    assert voice_path.exists()


def _route_to_client(monkeypatch: pytest.MonkeyPatch, client: WeChat | WeChatBot) -> WechatModule:
    """
    为单个实例装配通知路由，接收对象由现有宿主方法提供。
    """
    module = WechatModule()
    conf = NotificationConf(id="wechat-test", name="wechat-test", type="wechat", config={})
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    monkeypatch.setattr(module, "check_message", lambda *_: True)
    monkeypatch.setattr(module, "get_notification_userid", lambda *_: "member-1")
    return module


def test_plugin_native_image_survives_default_link_and_message_roundtrip(
    client: WeChat, http: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    插件默认详情链接、消息序列化与模块转发不会把显式原图变回图文卡片。
    """
    monkeypatch.setattr(plugin_base, "settings", SimpleNamespace(MP_DOMAIN=lambda path: f"https://mp.example/{path}"))
    plugin = SimpleNamespace(chain=Mock())
    _PluginBase.post_message(
        plugin, channel=NotificationChannel.Wechat, title="扫码登录", image=_PNG_DATA_URL, native_image=True,
    )
    message = plugin.chain.post_message.call_args.args[0]
    assert "#/plugins?tab=installed&id=" in message.link
    assert message.native_image is True
    module = _route_to_client(monkeypatch, client)

    module.post_message(Message.model_validate(message.model_dump()))

    sent = _payloads(http)
    assert [payload["msgtype"] for payload in sent] == ["text", "image"]
    assert all(payload["touser"] == "member-1" for payload in sent)
    assert "#/plugins" not in sent[0]["text"]["content"]


@pytest.mark.parametrize("native_image", [False, True])
def test_bot_mode_receives_same_native_image_request(
    monkeypatch: pytest.MonkeyPatch, native_image: bool,
) -> None:
    """
    机器人模式与自建应用使用同一原图开关，未启用时仍沿用旧通知参数。
    """
    bot = WeChatBot.__new__(WeChatBot)
    send = Mock()
    monkeypatch.setattr(bot, "send_msg", send)
    module = _route_to_client(monkeypatch, bot)
    message = Message(title="标题", image=_IMAGE_URL, link="https://detail.example", native_image=native_image)

    module.post_message(message)

    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert {key: kwargs[key] for key in ("title", "text", "image", "userid", "link")} == {
        "title": "标题", "text": None, "image": _IMAGE_URL,
        "userid": "member-1", "link": "https://detail.example",
    }
    assert kwargs.get("native_image", False) is native_image and callable(kwargs["part_sender"])


def test_native_image_respects_notification_filter(
    client: WeChat, http: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    显式原生图片不能绕过通知实例过滤规则。
    """
    module = _route_to_client(monkeypatch, client)
    monkeypatch.setattr(module, "check_message", lambda *_: False)

    module.post_message(Message(image=_PNG_DATA_URL, native_image=True))

    http.request.assert_not_called()
    http.post.assert_not_called()


def test_default_message_does_not_enable_native_image() -> None:
    """
    旧调用及其序列化数据默认保持图文行为。
    """
    assert Message(image=_IMAGE_URL).native_image is False
    assert Message.model_validate({"image": _IMAGE_URL}).native_image is False
