"""
群晖 Chat 空配置、令牌校验与消息负载兼容契约。
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest

import app.modules.synologychat.synologychat as client_module
from app.modules.synologychat import SynologyChatModule
from app.modules.synologychat.synologychat import SynologyChat


@pytest.mark.parametrize(
    "webhook,token",
    [(None, None), ("https://chat.invalid/hook", None), (None, "secret"),
     ("", "secret"), ("https://chat.invalid/hook", "")],
)
def test_incomplete_configuration_stays_disabled(monkeypatch, webhook, token):
    """
    配置缺项时所有公开状态和发送入口均安全返回，且任何令牌都不能通过。
    """
    transport = Mock()
    monkeypatch.setattr(client_module, "RequestUtils", transport)
    client = SynologyChat(SYNOLOGYCHAT_WEBHOOK=webhook, SYNOLOGYCHAT_TOKEN=token)
    assert client.get_state() is False
    assert client.check_token(None) is False
    assert client.check_token("secret") is False
    assert client.send_msg("title") is False
    assert client.send_medias_msg([object()], title="title") is False
    assert client.send_torrents_msg([object()], title="title") is None
    transport.assert_not_called()


@pytest.fixture
def configured_client(monkeypatch):
    """
    使用本地响应替身建立已配置客户端，禁止真实网络请求。
    """
    transport = Mock()
    transport.post_res.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"success": True}, text="", reason="OK",
    )
    transport.get_res.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"data": {"users": [{"user_id": 7, "deleted": False}]}},
    )
    monkeypatch.setattr(client_module, "RequestUtils", Mock(return_value=transport))
    client = SynologyChat(
        SYNOLOGYCHAT_WEBHOOK="https://chat.invalid/hook", SYNOLOGYCHAT_TOKEN="secret",
    )
    return client, transport


@pytest.mark.parametrize("token,expected", [(None, False), ("wrong", False), ("secret", True)])
def test_only_configured_token_is_accepted(configured_client, token, expected):
    """
    保留精确令牌匹配，不接受缺失或错误令牌。
    """
    client, transport = configured_client
    assert client.check_token(token) is expected
    transport.post_res.assert_not_called()
    transport.get_res.assert_not_called()


@pytest.mark.parametrize(
    "title,text,caption",
    [("head", None, "head"), (None, "body", "*None*\nbody"),
     ("head\nbody", None, "*head*\nbody"), ("head", "body\n\nline", "*head*\nbody\nline")],
)
def test_text_format_and_explicit_recipient_are_preserved(configured_client, title, text, caption):
    """
    可空标题和多行正文保持原格式，显式用户 ID 仍按整数编码。
    """
    client, transport = configured_client
    assert client.send_msg(title, text=text, userid="17") is True
    payload = json.loads(transport.post_res.call_args.kwargs["data"].removeprefix("payload="))
    assert unquote(payload["text"]) == caption
    assert payload["user_ids"] == [17]
    transport.get_res.assert_not_called()


def test_empty_message_does_not_send(configured_client):
    """
    标题正文均为空时仍直接拒绝发送。
    """
    client, transport = configured_client
    assert client.send_msg(None) is False
    transport.get_res.assert_not_called()
    transport.post_res.assert_not_called()


def test_bot_user_values_and_deleted_filter_are_preserved(configured_client):
    """
    可见用户的原始 ID 保持原值，只有 deleted 严格为 False 的条目被选中。
    """
    client, transport = configured_client
    transport.get_res.return_value.json = lambda: {
        "data": {"users": [
            {"user_id": 7, "deleted": False},
            {"user_id": "8", "deleted": False},
            {"deleted": False},
            {"user_id": 9, "deleted": True},
            {"user_id": 10, "deleted": 0},
        ]},
    }
    assert client.get_state() is True
    assert client.send_msg("title") is True
    payload = json.loads(transport.post_res.call_args.kwargs["data"].removeprefix("payload="))
    assert payload["user_ids"] == [7, "8", None]


class _IntUserId:
    """
    保留旧插件可能传入的整数转换协议。
    """

    def __int__(self):
        """
        返回用于入站消息路由的用户 ID。
        """
        return 17


class _IndexUserId:
    """
    覆盖 int 支持的索引转换协议。
    """

    def __index__(self):
        """
        返回用于入站消息路由的用户 ID。
        """
        return 17


@pytest.fixture
def incoming_parser(configured_client, monkeypatch):
    """
    把入站解析器连接到内存配置和已配置的本地客户端。
    """
    client, _ = configured_client
    module = SynologyChatModule()
    conf = SimpleNamespace(name="chat", config={})
    monkeypatch.setattr(module, "get_config", lambda _name: conf)
    monkeypatch.setattr(module, "get_instance", lambda _name: client)

    def parse(**fields):
        """
        用正常消息默认值构造表单，允许覆盖待验证字段。
        """
        form = {"token": "secret", "user_id": "17", "text": "hello", **fields}
        return module.message_parser("chat", None, form, None)

    return parse


@pytest.mark.parametrize(
    "value,expected",
    [(17, 17), ("17", 17), (b"17", 17), (bytearray(b"17"), 17),
     (memoryview(b"17"), 17), (17.9, 17), (True, 1), (_IntUserId(), 17), (_IndexUserId(), 17)],
)
def test_user_id_conversion_protocols_remain_supported(incoming_parser, value, expected):
    """
    保留 int 原先接受的字符串、缓冲区和数值协议，不因类型检查缩窄正常输入。
    """
    result = incoming_parser(user_id=value)
    assert result is not None
    assert result.userid == expected
    assert result.text == "hello"


@pytest.mark.parametrize("value", [None, object(), "not-a-number", float("inf")])
def test_invalid_user_ids_still_drop_message(incoming_parser, value):
    """
    不支持或不能完成整数转换的 ID 仍不能生成入站消息。
    """
    assert incoming_parser(user_id=value) is None


@pytest.mark.parametrize("token", [None, b"secret", "wrong", []])
def test_invalid_incoming_tokens_still_drop_message(incoming_parser, token):
    """
    解析入口拒绝缺失、非字符串或错误令牌。
    """
    assert incoming_parser(token=token) is None


def test_falsy_text_preserves_attachment_message(incoming_parser):
    """
    原先视为空文本的值仍允许有效附件作为消息内容。
    """
    result = incoming_parser(text=0, file_url="https://chat.invalid/image.png")
    assert result is not None
    assert result.text == ""
    assert result.images[0].ref == "https://chat.invalid/image.png"


def test_truthy_non_text_still_drops_message(incoming_parser):
    """
    缺少字符串操作的非空文本值仍被拒绝。
    """
    assert incoming_parser(text=["bad"]) is None
