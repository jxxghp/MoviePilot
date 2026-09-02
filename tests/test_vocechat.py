import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.modules.vocechat import VoceChatModule
from app.modules.vocechat.vocechat import VoceChat


@pytest.mark.parametrize(
    ("userid", "endpoint"),
    [
        ("123", "send_to_user/123"),
        ("UID#123", "send_to_user/123"),
        ("GID#456", "send_to_group/456"),
    ],
)
def test_send_msg_normalizes_vocechat_target(userid: str, endpoint: str):
    client = Mock()
    client.post_res.return_value = SimpleNamespace(status_code=200)
    vocechat = VoceChat(
        VOCECHAT_HOST="https://voce.example.com",
        VOCECHAT_API_KEY="test-key",
        VOCECHAT_CHANNEL_ID="456",
    )
    vocechat._client = client

    assert vocechat.send_msg(title="测试消息", userid=userid) is True
    assert client.post_res.call_args.args[0] == (
        f"https://voce.example.com/api/bot/{endpoint}"
    )


@pytest.mark.parametrize("userid", ["UID#", "GID#", "SID#123", "abc"])
def test_send_msg_rejects_invalid_vocechat_target(userid: str):
    client = Mock()
    vocechat = VoceChat(
        VOCECHAT_HOST="https://voce.example.com",
        VOCECHAT_API_KEY="test-key",
        VOCECHAT_CHANNEL_ID="456",
    )
    vocechat._client = client

    assert vocechat.send_msg(title="测试消息", userid=userid) is False
    client.post_res.assert_not_called()


def _parse_group_message(*, mentions=None, bot_id=None):
    module = VoceChatModule()
    # 生产通知配置使用大写 VOCECHAT_CHANNEL_ID；此前读取小写键导致
    # 所有群消息被误判为私聊，并向发送者逐条私聊回复。
    config = {"VOCECHAT_CHANNEL_ID": "2"}
    if bot_id is not None:
        config["VOCECHAT_BOT_ID"] = str(bot_id)
    body = json.dumps(
        {
            "detail": {
                "type": "normal",
                "content_type": "text/plain",
                "content": "群消息",
                "properties": {"mentions": mentions} if mentions is not None else None,
            },
            "from_uid": 10,
            "target": {"gid": 2},
        }
    )
    with patch.object(
        module,
        "get_config",
        return_value=SimpleNamespace(name="vocechat-test", config=config),
    ):
        return module.message_parser("vocechat-test", body, {}, {})


def test_group_message_without_mention_is_ignored():
    assert _parse_group_message(mentions=[]) is None


def test_group_message_with_mention_replies_in_group():
    message = _parse_group_message(mentions=[5])
    assert message is not None
    assert message.userid == "GID#2"


def test_group_message_only_accepts_configured_bot_mention():
    assert _parse_group_message(mentions=[6], bot_id=5) is None
    message = _parse_group_message(mentions=[5, 6], bot_id=5)
    assert message is not None
    assert message.userid == "GID#2"
