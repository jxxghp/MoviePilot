"""
验证微信渠道客户端的会话、消息和配置行为。
"""

import json
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.modules.wechatclawbot import WechatClawBotModule
from app.modules.wechatclawbot.wechatclawbot import ILinkClient, WechatClawBot

CLIENT_BASE_URL = "https://ilinkai.weixin.qq.com"


def _create_client(**kwargs) -> ILinkClient:
    """
    构造指向 iLink 服务端的客户端实例。
    """
    return ILinkClient(base_url=CLIENT_BASE_URL, **kwargs)


def _empty_state() -> dict:
    """
    构造未登录的微信 ClawBot 持久化状态。
    """
    return {
        "bot_token": None,
        "account_id": None,
        "sync_buf": None,
        "qrcode": {},
        "known_targets": {},
        "user_context_tokens": {},
        "base_url": CLIENT_BASE_URL,
    }


def _create_bot() -> WechatClawBot:
    """
    构造不自动轮询的微信 ClawBot 实例。
    """
    with patch.object(WechatClawBot, "_load_state", return_value=_empty_state()):
        return WechatClawBot(name="wechatclawbot-test", auto_start_polling=False)


def _module_config() -> SimpleNamespace:
    """
    构造消息解析所需的渠道配置。
    """
    return SimpleNamespace(name="wechatclawbot-test", config={})


def test_ilink_parse_incoming_uses_seq_as_message_id_fallback():
    """
    入站消息缺少独立消息标识时，使用序列号作为去重标识。
    """
    client = _create_client()

    message = client._parse_incoming(
        {
            "seq": 123456,
            "from_user_id": "wxid_user_1",
            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        }
    )

    assert message is not None
    assert message.message_id == "123456"
    assert message.text == "你好"


def test_ilink_parse_incoming_extracts_item_list_media():
    """
    iLink 下行媒体放在 item_list 子项的 media 中，应解析为可直接下载的附件引用。
    """
    client = _create_client()
    image_url = "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=image-1"
    voice_url = "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=voice-1"
    file_url = "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=file-1"
    video_url = "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=video-1"
    media_aes_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

    message = client._parse_incoming(
        {
            "message_id": "msg-media-1",
            "from_user_id": "wxid_user_1",
            "message_type": 1,
            "item_list": [
                {"type": 1, "text_item": {"text": "看这张图"}},
                {
                    "type": 2,
                    "image_item": {
                        "aeskey": "0123456789abcdef0123456789abcdef",
                        "media": {"full_url": image_url, "aes_key": media_aes_key},
                    },
                },
                {
                    "type": 3,
                    "voice_item": {"media": {"full_url": voice_url, "aes_key": media_aes_key}},
                },
                {
                    "type": 4,
                    "file_item": {
                        "file_name": "报告.pdf",
                        "size": 1024,
                        "media": {"full_url": file_url, "aes_key": media_aes_key},
                    },
                },
                {
                    "type": 5,
                    "video_item": {"media": {"full_url": video_url, "aes_key": media_aes_key}},
                },
            ],
        }
    )

    assert message is not None
    assert message.text == "看这张图"
    assert len(message.images) == 1
    image_payload = WechatClawBot._decode_ref_payload(message.images[0]["ref"], "image")
    assert image_payload["url"] == image_url
    assert image_payload["aeskey"] == "0123456789abcdef0123456789abcdef"
    assert len(message.audio_refs) == 1
    voice_payload = WechatClawBot._decode_ref_payload(message.audio_refs[0], "voice")
    assert voice_payload["url"] == voice_url
    assert voice_payload["aeskey"] == media_aes_key
    assert len(message.files) == 2
    file_payload = WechatClawBot._decode_ref_payload(message.files[0]["ref"], "file")
    assert file_payload["url"] == file_url
    assert file_payload["name"] == "报告.pdf"
    assert message.files[0]["size"] == 1024
    video_payload = WechatClawBot._decode_ref_payload(message.files[1]["ref"], "file")
    assert video_payload["url"] == video_url
    assert message.files[1]["name"] == "video.mp4"


def test_ilink_parse_incoming_keeps_legacy_file_item_types():
    """
    历史报文把文件放在 type=5/6 的 file_item 中，仍需保持兼容。
    """
    client = _create_client()
    file_url = "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=legacy-1"

    message = client._parse_incoming(
        {
            "message_id": "msg-file-legacy",
            "from_user_id": "wxid_user_1",
            "item_list": [
                {
                    "type": 6,
                    "file_item": {
                        "name": "archive.zip",
                        "media": {"full_url": file_url},
                    },
                }
            ],
        }
    )

    assert message is not None
    assert len(message.files) == 1
    file_payload = WechatClawBot._decode_ref_payload(message.files[0]["ref"], "file")
    assert file_payload["url"] == file_url
    assert file_payload["name"] == "archive.zip"


def test_ilink_extract_updates_keeps_empty_sync_buf():
    """
    提取更新时保留服务端返回的空同步游标，不回退为旧值。
    """
    client = _create_client(bot_token="token", sync_buf="cursor-old")

    items, sync_buf = client._extract_updates(
        {
            "ret": 0,
            "get_updates_buf": "",
            "msgs": [
                {
                    "message_id": "msg-1001",
                    "from_user_id": "wxid_user_1",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            ],
        }
    )

    assert sync_buf == ""
    assert len(items) == 1


def test_ilink_poll_updates_resets_sync_buf_when_server_returns_empty_cursor():
    """
    轮询收到空游标后重置本地同步状态，避免继续使用旧游标。
    """
    client = _create_client(bot_token="token", sync_buf="cursor-old")
    response = MagicMock()
    response.json.return_value = {
        "ret": 0,
        "get_updates_buf": "",
        "msgs": [
            {
                "message_id": "msg-1001",
                "from_user_id": "wxid_user_1",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            }
        ],
    }

    with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
        messages, sync_buf, result = client.poll_updates()

    assert result["success"]
    assert sync_buf == ""
    assert client.sync_buf == ""
    assert len(messages) == 1


def test_ilink_poll_updates_accepts_canonical_payload_without_explicit_success_flag():
    """
    标准更新结构即使没有显式成功标志，也可被正常处理。
    """
    client = _create_client(bot_token="token", sync_buf="cursor-old")
    response = MagicMock()
    response.json.return_value = {
        "sync_buf": "cursor-new",
        "msgs": [
            {
                "message_id": "msg-1002",
                "from_user_id": "wxid_user_2",
                "item_list": [{"type": 1, "text_item": {"text": "收到"}}],
            }
        ],
    }

    with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
        messages, sync_buf, result = client.poll_updates()

    assert result["success"]
    assert sync_buf == "cursor-new"
    assert client.sync_buf == "cursor-new"
    assert len(messages) == 1
    assert messages[0].text == "收到"


def test_ilink_poll_updates_rejects_noncanonical_nested_success_payload():
    """
    拒绝非标准嵌套成功结构，避免把不受支持的消息格式当作更新。
    """
    client = _create_client(bot_token="token", sync_buf="cursor-old")
    response = MagicMock()
    response.json.return_value = {
        "ret": 0,
        "data": {
            "get_updates_buf": "cursor-new",
            "messages": [
                {
                    "message_id": "msg-1001",
                    "from_user_id": "wxid_user_1",
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            ],
        },
    }

    with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
        messages, sync_buf, result = client.poll_updates()

    assert not result["success"]
    assert result["message"] == "轮询响应结构非官方，缺少顶层 msgs 字段"
    assert sync_buf == "cursor-old"
    assert client.sync_buf == "cursor-old"
    assert messages == []


def test_ilink_poll_updates_rejects_failed_payload_even_if_it_contains_messages():
    """
    响应已声明失败时，即使附带消息也不能作为成功更新消费。
    """
    client = _create_client(bot_token="token", sync_buf="cursor-old")
    failed_response = MagicMock()
    failed_response.json.return_value = {
        "ret": -2,
        "errmsg": "cursor invalid",
        "data": {
            "sync_buf": "cursor-old",
            "messages": [
                {
                    "message_id": "msg-dup-1",
                    "from_user_id": "wxid_user_1",
                    "item_list": [{"type": 1, "text_item": {"text": "旧消息"}}],
                }
            ],
        },
    }

    with patch(
        "app.modules.wechatclawbot.wechatclawbot.RequestUtils.post",
        return_value=failed_response,
    ) as mock_post:
        messages, sync_buf, result = client.poll_updates()

    assert not result["success"]
    assert result["message"] == "cursor invalid"
    assert sync_buf == "cursor-old"
    assert client.sync_buf == "cursor-old"
    assert messages == []
    mock_post.assert_called_once()
    request_body = mock_post.call_args.kwargs["json"]
    assert "get_updates_buf" in request_body
    assert "sync_buf" not in request_body
    assert "syncBuf" not in request_body


def test_ilink_test_connection_accepts_getconfig_ilink_user_id_limitation():
    """
    连通性自检缺少额外用户参数的提示不等同于连接失败。
    """
    client = _create_client(bot_token="token")
    response = MagicMock()
    response.json.return_value = {
        "ret": -1,
        "errmsg": "ilink_user_id required",
    }

    with patch("app.modules.wechatclawbot.wechatclawbot.RequestUtils.post", return_value=response):
        ok, message = client.test_connection()

    # `ilink_user_id required` 仅表示自检接口缺少额外参数，不代表连接失败：视为连接正常
    assert ok
    assert message == "连接正常"


def test_wechatclawbot_message_parser_deduplicates_message_id():
    """
    渠道消息解析按消息标识去重，重复投递不再次处理。
    """
    module = WechatClawBotModule()
    body = json.dumps(
        {
            "__channel__": "wechatclawbot",
            "userid": "wxid_user_1",
            "username": "tester",
            "message_id": "msg-1001",
            "text": "刷新订阅",
        }
    )

    with patch.object(module, "get_config", return_value=_module_config()):
        first = module.message_parser(
            source="wechatclawbot-test",
            body=body,
            form={},
            args={},
        )
        second = module.message_parser(
            source="wechatclawbot-test",
            body=body,
            form={},
            args={},
        )

    assert first is not None
    assert first.message_id == "msg-1001"
    assert second is None


def test_wechatclawbot_message_parser_accepts_image_only_message():
    """
    图片入站消息（无文本）应进入 Agent 链路，而不是被判为无效消息丢弃。
    """
    module = WechatClawBotModule()
    client = _create_client()
    parsed_message = client._parse_incoming(
        {
            "message_id": "msg-image-only",
            "from_user_id": "wxid_user_1",
            "item_list": [
                {
                    "type": 2,
                    "image_item": {
                        "aeskey": "0123456789abcdef0123456789abcdef",
                        "media": {
                            "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/download?fileid=image-only",
                            "aes_key": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
                        },
                    },
                }
            ],
        }
    )
    assert parsed_message is not None

    with patch.object(module, "get_config", return_value=_module_config()):
        parsed = module.message_parser(
            source="wechatclawbot-test",
            body=json.dumps(parsed_message.to_message_payload()),
            form={},
            args={},
        )

    assert parsed is not None
    assert parsed.images is not None
    assert parsed.images[0].ref.startswith("wxclaw://image/")
    assert not parsed.text


def test_wechatclawbot_message_parser_logs_unrecognized_content():
    """
    无法识别的入站内容应留下诊断日志，而不是静默丢弃。
    """
    module = WechatClawBotModule()
    body = json.dumps(
        {
            "__channel__": "wechatclawbot",
            "userid": "wxid_user_1",
            "message_id": "msg-unknown-1",
            "item_list": [{"type": 99, "unknown_item": {"foo": "bar"}}],
        }
    )

    with (
        patch.object(module, "get_config", return_value=_module_config()),
        patch("app.modules.wechatclawbot.logger") as mock_logger,
    ):
        parsed = module.message_parser(
            source="wechatclawbot-test",
            body=body,
            form={},
            args={},
        )

    assert parsed is None
    mock_logger.info.assert_called_once()
    assert "未识别到有效内容" in mock_logger.info.call_args.args[0]


def test_wechatclawbot_send_msg_uses_plain_text_payload():
    """
    普通机器人消息以纯文本发送，保留标题、正文与链接。
    """
    bot = _create_bot()
    mock_client = MagicMock()
    mock_client.send_text.return_value = True

    with patch.object(bot, "_build_client", return_value=mock_client):
        result = bot.send_msg(
            title="测试标题",
            text="测试正文",
            userid="wxid_user_1",
            link="https://example.com/detail",
        )

    assert result
    mock_client.send_text.assert_called_once_with(
        to_user="wxid_user_1",
        text="测试标题\n\n测试正文\n\n查看详情：https://example.com/detail",
        context_token=None,
    )


def test_wechatclawbot_send_msg_prefers_text_when_text_and_image_coexist():
    """
    正文和配图同时存在时优先发送正文，不额外加载远端图片。
    """
    bot = _create_bot()
    mock_client = MagicMock()
    mock_client.send_text.return_value = True
    mock_client.send_image_png.return_value = True
    mock_client.send_image_text_png.return_value = True

    with (
        patch.object(bot, "_build_client", return_value=mock_client),
        patch.object(bot, "_load_remote_image", return_value=b"image-bytes") as mock_load_image,
    ):
        result = bot.send_msg(
            title="测试标题",
            text="测试正文",
            image="https://example.com/test.png",
            userid="wxid_user_1",
        )

    assert result
    mock_load_image.assert_not_called()
    mock_client.send_text.assert_called_once_with(
        to_user="wxid_user_1",
        text="测试标题\n\n测试正文",
        context_token=None,
    )
    mock_client.send_image_png.assert_not_called()
    mock_client.send_image_text_png.assert_not_called()


def test_wechatclawbot_send_file_prefers_image_when_image_file_has_caption():
    """
    图片文件带说明时仍按图片发送，避免退化为纯文本消息。
    """
    bot = _create_bot()
    mock_client = MagicMock()
    mock_client.send_text.return_value = True
    mock_client.send_image_png.return_value = True

    with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
        image_file.write(b"\x89PNG\r\n\x1a\nfake-png")
        image_file.flush()
        with (
            patch.object(bot, "_build_client", return_value=mock_client),
            patch.object(bot, "_guess_mime_type", return_value="image/png"),
        ):
            result = bot.send_file(
                file_path=image_file.name,
                title="图片标题",
                text="图片说明",
                userid="wxid_user_1",
            )

    assert result
    mock_client.send_text.assert_not_called()
    mock_client.send_image_png.assert_called_once_with(
        to_user="wxid_user_1",
        image_bytes=b"\x89PNG\r\n\x1a\nfake-png",
        context_token=None,
    )


def test_wechatclawbot_send_file_prefers_file_when_generic_file_has_caption():
    """
    普通文件带说明时仍发送文件内容，避免丢失附件。
    """
    bot = _create_bot()
    mock_client = MagicMock()
    mock_client.send_text.return_value = True
    mock_client.send_file_bytes.return_value = True

    with tempfile.NamedTemporaryFile(suffix=".txt") as text_file:
        text_file.write(b"plain-text")
        text_file.flush()
        with (
            patch.object(bot, "_build_client", return_value=mock_client),
            patch.object(bot, "_guess_mime_type", return_value="text/plain"),
        ):
            result = bot.send_file(
                file_path=text_file.name,
                file_name="report.txt",
                title="文件标题",
                text="文件说明",
                userid="wxid_user_1",
            )

    assert result
    mock_client.send_text.assert_not_called()
    mock_client.send_file_bytes.assert_called_once_with(
        to_user="wxid_user_1",
        file_bytes=b"plain-text",
        file_name="report.txt",
        mime_type="text/plain",
        context_token=None,
    )
