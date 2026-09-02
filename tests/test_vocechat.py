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


def _parse_group_message(*, mentions=None, bot_id=None, mention_only=None, gid=2):
    module = VoceChatModule()
    # 生产通知配置使用大写 VOCECHAT_CHANNEL_ID；此前读取小写键导致
    # 所有群消息被误判为私聊，并向发送者逐条私聊回复。
    config = {"VOCECHAT_CHANNEL_ID": "2"}
    if bot_id is not None:
        config["VOCECHAT_BOT_ID"] = str(bot_id)
    if mention_only is not None:
        config["VOCECHAT_MENTION_ONLY"] = mention_only
    body = json.dumps(
        {
            "detail": {
                "type": "normal",
                "content_type": "text/plain",
                "content": "群消息",
                "properties": {"mentions": mentions} if mentions is not None else None,
            },
            "from_uid": 10,
            "target": {"gid": gid},
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
    message = _parse_group_message(mentions=[5], bot_id=5)
    assert message is not None
    assert message.userid == "GID#2"


def test_group_message_only_accepts_configured_bot_mention():
    assert _parse_group_message(mentions=[6], bot_id=5) is None
    message = _parse_group_message(mentions=[5, 6], bot_id=5)
    assert message is not None
    assert message.userid == "GID#2"


def test_group_message_can_disable_mention_only_filter():
    message = _parse_group_message(mentions=[], mention_only=False)
    assert message is not None
    assert message.userid == "GID#2"


def test_group_message_from_unconfigured_channel_is_ignored():
    """其他频道的消息不得降级成私聊回复给发送者。"""
    assert _parse_group_message(mentions=[5], gid=3) is None


def test_message_without_explicit_target_type_is_ignored():
    """未知目标结构不得降级成私聊回复给发送者。"""
    module = VoceChatModule()
    body = json.dumps(
        {
            "detail": {
                "type": "normal",
                "content_type": "text/plain",
                "content": "未知目标消息",
            },
            "from_uid": 10,
            "target": {},
        }
    )
    with patch.object(
        module,
        "get_config",
        return_value=SimpleNamespace(
            name="vocechat-test",
            config={"VOCECHAT_CHANNEL_ID": "2"},
        ),
    ):
        assert module.message_parser("vocechat-test", body, {}, {}) is None
