"""
飞书列表通知保留用户标识的历史目标解析与异常边界。
"""

from unittest.mock import Mock

import pytest

from app.modules.feishu.feishu import Feishu
from app.schemas.message import Message


@pytest.mark.parametrize("method", ["send_medias_message", "send_torrents_message"])
@pytest.mark.parametrize("userid, expected_target", [("ou_user", "ou_user"), (0, "ou_default"), (12, None)])
def test_list_notification_preserves_userid_target_boundary(method, userid, expected_target):
    """
    字符串发往用户，零值沿用默认目标，非零整数在通知内部捕获失败而不外抛。
    """
    client = Feishu.__new__(Feishu)
    client._default_open_id = "ou_default"
    client._default_chat_id = None
    client._user_receive_id_type_mapping = {}
    client._user_chat_mapping = {}
    client._upload_remote_image = Mock(return_value=None)
    client._build_notification_cards = Mock(return_value=[{"schema": "2.0"}])
    client._send_message = Mock(return_value={"success": True, "chat_id": "oc_chat"})

    result = getattr(client, method)(Message(title="测试", userid=userid), [])

    if expected_target is None:
        assert result == {"success": False}
        client._send_message.assert_not_called()
    else:
        assert result["success"] is True
        assert client._send_message.call_args.args[:2] == (expected_target, "open_id")


def test_reply_does_not_resolve_integer_userid():
    """
    明确回复原消息时不解析用户标识，非零整数仍允许成功回复。
    """
    client = Feishu.__new__(Feishu)
    client._default_chat_id = None
    client._user_chat_mapping = {}
    client._upload_remote_image = Mock(return_value=None)
    client._build_notification_cards = Mock(return_value=[{"schema": "2.0"}])
    client._reply_message = Mock(return_value={"success": True, "chat_id": "oc_reply"})
    client._resolve_target = Mock(side_effect=AssertionError("回复不可解析目标"))

    result = client.send_notification(Message(title="测试"), userid=12, original_message_id="om_reply")

    assert result == {"success": True, "chat_id": "oc_reply"}
    client._resolve_target.assert_not_called()
