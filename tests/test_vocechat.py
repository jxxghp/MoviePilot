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


@pytest.mark.parametrize(
    ("target", "content", "expected_userid", "endpoint"),
    [
        ({"gid": 2}, "搜索 测试影片", "GID#2", "send_to_group/2"),
        ({"gid": 2}, "1", "GID#2", "send_to_group/2"),
        ({"uid": 1}, "搜索 测试影片", "UID#7", "send_to_user/7"),
    ],
)
def test_message_parser_routes_reply_to_origin(
    target: dict, content: str, expected_userid: str, endpoint: str
):
    """频道搜索与序号交互回原频道，私聊回复发起用户。"""
    module = VoceChatModule()
    vocechat = VoceChat(
        VOCECHAT_HOST="https://voce.example.com",
        VOCECHAT_API_KEY="test-key",
        VOCECHAT_CHANNEL_ID="2",
    )
    client = Mock()
    client.post_res.return_value = SimpleNamespace(status_code=200)
    vocechat._client = client
    body = json.dumps(
        {
            "detail": {"type": "normal", "content_type": "text/plain", "content": content},
            "from_uid": 7,
            "target": target,
        }
    )

    with patch.object(
        module,
        "get_config",
        return_value=SimpleNamespace(
            name="vocechat-test", config={"VOCECHAT_CHANNEL_ID": "2"}
        ),
    ), patch.object(module, "get_instance", return_value=vocechat):
        message = module.message_parser("vocechat-test", body, {}, {})

    assert message is not None
    assert message.userid == expected_userid
    assert message.text == content
    assert vocechat.send_msg(title="搜索结果", userid=message.userid) is True
    assert client.post_res.call_args.args[0] == (
        f"https://voce.example.com/api/bot/{endpoint}"
    )
