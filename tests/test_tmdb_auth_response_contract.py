"""
认证链使用 TMDb 已解析的 JSON 字典，而不是属性包装对象。
"""

from unittest.mock import Mock, call

import pytest

from app.modules.themoviedb.tmdbv3api.exceptions import TMDbException
from app.modules.themoviedb.tmdbv3api.objs.auth import Authentication


def test_authentication_reads_json_credentials_and_preserves_request_order(monkeypatch):
    """
    真实字典中的 token 和 session 必须按顺序传递，注销后清空会话。
    """
    request = Mock(side_effect=[
        {"success": True, "expires_at": "2030-01-01 00:00:00 UTC", "request_token": "token"},
        {"success": True},
        {"success": True, "session_id": "session"},
        {"success": True},
    ])
    monkeypatch.setattr(Authentication, "_request_obj", request)
    client = Authentication("user", "password")
    try:
        assert client.request_token == "token"
        assert client.expires_at == "2030-01-01 00:00:00 UTC"
        assert client.session_id == "session"
        client.delete_session()
        assert not client.has_session
        assert request.call_args_list == [
            call("/authentication/token/new"),
            call("/authentication/token/validate_with_login", method="POST", json={
                "username": "user", "password": "password", "request_token": "token",
            }),
            call("/authentication/session/new", method="POST", json={"request_token": "token"}),
            call("/authentication/session", method="DELETE", json={"session_id": "session"}),
        ]
    finally:
        client.close()


@pytest.mark.parametrize("value", [None, "", 42, [], {}])
@pytest.mark.parametrize("field", ["request_token", "session_id"])
def test_authentication_rejects_missing_or_invalid_credentials(monkeypatch, field, value):
    """
    失败的凭据解析应终止认证，不能把空值或非字符串发给下一步。
    """
    responses = [{field: value}]
    if field == "session_id":
        responses = [{"request_token": "token"}, {"success": True}, *responses]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(Authentication, "_request_obj", request)
    with pytest.raises(TMDbException, match=field):
        Authentication("user", "password")
    assert request.call_count == len(responses)
