"""
Telegram Bot API 精简客户端测试：请求编码、错误语义、文件上传和长轮询循环。
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from app.modules.telegram.botapi import TelegramApiError, TelegramBotApi, TelegramNetworkError


def _response(payload=None, status_code=200, text=None):
    """构造 requests.Response 替身；payload 为 None 时 json() 抛 ValueError。"""
    def _json():
        if payload is None:
            raise ValueError(text)
        return payload

    return SimpleNamespace(status_code=status_code, json=_json)


@pytest.fixture
def post_res():
    """拦截客户端的全部出站 POST，默认返回成功结果。"""
    with patch("app.modules.telegram.botapi.RequestUtils.post_res") as mock_post:
        mock_post.return_value = _response({"ok": True, "result": {"message_id": 7}})
        yield mock_post


def test_default_api_uses_official_host_and_proxy(post_res):
    """官方地址走全局代理，自定义地址只取主机并跳过代理。"""
    proxy = {"https": "http://127.0.0.1:7890"}
    client = TelegramBotApi("1:abc", proxy=proxy)
    assert client.proxy == proxy
    assert client.file_url("a/b.jpg") == "https://api.telegram.org/file/bot1:abc/a/b.jpg"
    client.get_me()
    assert post_res.call_args.args[0] == "https://api.telegram.org/bot1:abc/getMe"

    custom = TelegramBotApi("1:abc", api_url="http://tg.lan:8081/ignored/path", proxy=proxy)
    assert custom.proxy is None
    custom.get_me()
    assert post_res.call_args.args[0] == "http://tg.lan:8081/bot1:abc/getMe"
    assert custom.file_url("x") == "http://tg.lan:8081/file/bot1:abc/x"


def test_request_verifies_tls():
    """Bot token 随每次请求发送，客户端不能沿用 RequestUtils 的默认不校验证书。"""
    with patch("app.modules.telegram.botapi.RequestUtils") as mock_utils:
        TelegramBotApi("1:abc")
    assert mock_utils.call_args.kwargs["verify"] is True
    assert mock_utils.call_args.kwargs["use_session"] is True


def test_send_message_encodes_bot_api_parameters(post_res):
    """嵌套结构编码为 JSON，旧式回复和预览参数转换为当前 Bot API 字段，None 不发送。"""
    client = TelegramBotApi("1:abc", parse_mode="MarkdownV2")
    result = client.send_message(
        chat_id=10001,
        text="你好",
        reply_markup={"inline_keyboard": [[{"text": "按钮", "callback_data": "x"}]]},
        disable_web_page_preview=True,
        reply_to_message_id="42",
        message_thread_id=None,
    )

    assert result == {"message_id": 7}
    data = post_res.call_args.kwargs["data"]
    assert data["chat_id"] == 10001
    assert data["parse_mode"] == "MarkdownV2"
    assert json.loads(data["reply_markup"])["inline_keyboard"][0][0]["text"] == "按钮"
    assert json.loads(data["link_preview_options"]) == {"is_disabled": True}
    assert json.loads(data["reply_parameters"]) == {"message_id": 42}
    assert "message_thread_id" not in data
    assert post_res.call_args.kwargs["files"] is None
    assert post_res.call_args.kwargs["raise_exception"] is True


def test_plain_parse_mode_is_omitted_and_rich_edit_has_no_parse_mode(post_res):
    """空 parse_mode 表示纯文本；Rich Message 编辑不带默认 parse_mode。"""
    client = TelegramBotApi("1:abc", parse_mode="MarkdownV2")
    client.send_message(chat_id=1, text="plain", parse_mode="")
    assert "parse_mode" not in post_res.call_args.kwargs["data"]

    client.edit_message_text(chat_id=1, message_id=2, rich_message={"html": "<b>x</b>"})
    data = post_res.call_args.kwargs["data"]
    assert "parse_mode" not in data and "text" not in data
    assert json.loads(data["rich_message"]) == {"html": "<b>x</b>"}

    client.send_message(chat_id=1, text="flag", disable_web_page_preview=False)
    assert json.loads(post_res.call_args.kwargs["data"]["link_preview_options"]) == {
        "is_disabled": False
    }


def test_media_upload_routes_by_input_type(post_res):
    """字符串按 file_id/URL 发送，bytes 和 (文件名, 内容) 走 multipart。"""
    client = TelegramBotApi("1:abc")
    client.send_photo(chat_id=1, photo="https://img.example/a.jpg", caption="c")
    assert post_res.call_args.kwargs["data"]["photo"] == "https://img.example/a.jpg"
    assert post_res.call_args.kwargs["files"] is None

    client.send_photo(chat_id=1, photo=b"raw")
    assert post_res.call_args.kwargs["files"] == {"photo": b"raw"}

    client.send_document(chat_id=1, document=("报告.txt", b"doc"))
    assert post_res.call_args.args[0].endswith("/sendDocument")
    assert post_res.call_args.kwargs["files"] == {"document": ("报告.txt", b"doc")}

    client.send_photo(chat_id=1, photo=("", b"img"))
    assert post_res.call_args.kwargs["files"] == {"photo": ("photo", b"img")}


def test_api_error_keeps_telegram_description(post_res):
    """ok=false 抛出含 description 的异常，调用方据此识别可忽略的编辑错误。"""
    post_res.return_value = _response({
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: message is not modified",
    })
    client = TelegramBotApi("1:abc")
    with pytest.raises(TelegramApiError) as exc:
        client.edit_message_text(chat_id=1, message_id=2, text="same")
    assert exc.value.error_code == 400
    assert "message is not modified" in str(exc.value).lower()

    post_res.return_value = _response(None, status_code=502, text="<html>")
    with pytest.raises(TelegramApiError) as exc:
        client.get_me()
    assert exc.value.error_code == 502


def test_polling_dispatches_in_order_and_survives_handler_errors():
    """更新按顺序分发，offset 确认到最后一条；单条处理失败不影响后续更新。"""
    client = TelegramBotApi("1:abc")
    batches = [
        [{"update_id": 10, "n": 1}, {"update_id": 11, "n": 2}],
        [{"update_id": 12, "n": 3}],
    ]
    offsets = []
    handled = []

    def fake_get_updates(offset, timeout):
        offsets.append(offset)
        if batches:
            return batches.pop(0)
        client.stop_polling(0)
        return []

    def handler(update):
        handled.append(update["n"])
        if update["n"] == 1:
            raise RuntimeError("boom")

    with patch.object(client, "get_updates", side_effect=fake_get_updates):
        client.polling(handler, long_polling_timeout=1)

    assert handled == [1, 2, 3]
    assert offsets == [None, 12, 13]


def test_polling_backs_off_on_failure_and_stops_promptly():
    """拉取失败进入退避等待，stop_polling 能立即打断等待并退出循环。"""
    client = TelegramBotApi("1:abc")
    calls = threading.Event()

    def failing_get_updates(offset, timeout):
        calls.set()
        raise ConnectionError("network down")

    with patch.object(client, "get_updates", side_effect=failing_get_updates):
        thread = threading.Thread(
            target=client.polling, args=(lambda update: None,), kwargs={"long_polling_timeout": 1}
        )
        thread.start()
        assert calls.wait(1)
        client.stop_polling(0)
        thread.join(1)
    assert not thread.is_alive()


def test_get_updates_requests_only_handled_update_types(post_res):
    """getUpdates 只订阅消息和按钮回调，读取超时大于长轮询等待。"""
    post_res.return_value = _response({"ok": True, "result": []})
    client = TelegramBotApi("1:abc")
    assert client.get_updates(offset=5, timeout=8) == []
    data = post_res.call_args.kwargs["data"]
    assert data["offset"] == 5 and data["timeout"] == 8
    assert json.loads(data["allowed_updates"]) == ["message", "callback_query"]
    connect_timeout, read_timeout = post_res.call_args.kwargs["timeout"]
    assert read_timeout > 8 and connect_timeout > 0


def test_network_error_hides_bot_token(post_res):
    """requests 异常文本含完整地址，抛出前必须去掉 token 且不保留异常链。"""
    token = "123456:SECRET-token"
    post_res.side_effect = requests.ConnectTimeout(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): url: /bot{token}/getMe"
    )
    client = TelegramBotApi(token)
    with pytest.raises(TelegramNetworkError) as exc:
        client.get_me()
    assert token not in str(exc.value)
    assert "<token>" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
