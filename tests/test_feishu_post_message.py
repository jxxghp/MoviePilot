import json
from unittest.mock import MagicMock, patch

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.modules.feishu.feishu import Feishu  # noqa: E402


def _build_feishu_client() -> Feishu:
    """构造不会启动飞书长连接的测试客户端。"""
    with (
        patch.object(Feishu, "_build_api_client", return_value=MagicMock()),
        patch.object(Feishu, "_start_ws_client"),
    ):
        return Feishu(
            FEISHU_APP_ID="test_app_id",
            FEISHU_APP_SECRET="test_app_secret",
            name="feishu-test",
        )


def test_on_message_extracts_localized_post_text_and_images():
    """飞书富文本事件应提取标题、正文、链接和图片引用。"""
    client = _build_feishu_client()
    message = {
        "message_id": "om_post_evt",
        "chat_id": "oc_chat_evt",
        "chat_type": "p2p",
        "message_type": "post",
        "content": json.dumps(
            {
                "post": {
                    "zh_cn": {
                        "title": "搜索请求",
                        "content": [
                            [
                                {"tag": "text", "text": "/search "},
                                {
                                    "tag": "a",
                                    "text": "MoviePilot",
                                    "href": "https://example.com/moviepilot",
                                },
                            ],
                            [
                                {"tag": "at", "user_name": "管理员"},
                                {"tag": "text", "text": " 请处理"},
                            ],
                            [{"tag": "img", "image_key": "img_v2_post"}],
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
    }
    event = {
        "sender": {"sender_id": {"open_id": "ou_user_evt", "user_id": None}},
        "message": message,
    }

    with patch.object(client, "_forward_to_message_chain") as forward:
        client._on_message(event)

    payload = forward.call_args.args[0]
    assert payload["text"] == (
        "搜索请求\n"
        "/search MoviePilot https://example.com/moviepilot\n"
        "@管理员 请处理"
    )
    assert payload["images"][0]["ref"] == "feishu://image/om_post_evt/img_v2_post"


def test_parse_message_content_supports_direct_post_body():
    """飞书富文本直接正文结构应被转换为普通文本。"""
    message = {
        "message_id": "om_post_direct",
        "message_type": "post",
        "content": json.dumps(
            {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "/help"}],
                    [{"tag": "text", "text": "第二行"}],
                ],
            },
            ensure_ascii=False,
        ),
    }

    text, images, audio_refs, files = Feishu._parse_message_content(message)

    assert text == "/help\n第二行"
    assert images is None
    assert audio_refs is None
    assert files is None
