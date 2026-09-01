from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
