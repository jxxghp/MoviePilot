from unittest.mock import Mock, patch

from app.modules.emby.emby import Emby


def _resolve_user(users: list[dict], requested_username: str, configured_username: str):
    emby = Emby.__new__(Emby)
    emby._host = "http://emby.local/"
    emby._apikey = "test-api-key"
    emby._username = configured_username

    response = Mock()
    response.json.return_value = users
    with patch("app.modules.emby.emby.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = response
        return emby.get_user(requested_username)


def test_get_user_prefers_requested_username():
    """指定用户名存在时应优先使用该用户。"""
    result = _resolve_user(
        users=[
            {"Id": "requested-id", "Name": "mp-user", "Policy": {}},
            {"Id": "configured-id", "Name": "configured-user", "Policy": {}},
            {"Id": "admin-id", "Name": "admin", "Policy": {"IsAdministrator": True}},
        ],
        requested_username="mp-user",
        configured_username="configured-user",
    )

    assert result == "requested-id"


def test_get_user_falls_back_to_configured_username():
    """指定用户名不存在时应回退媒体服务器配置用户。"""
    result = _resolve_user(
        users=[
            {"Id": "configured-id", "Name": "configured-user", "Policy": {}},
            {"Id": "admin-id", "Name": "admin", "Policy": {"IsAdministrator": True}},
        ],
        requested_username="missing-mp-user",
        configured_username="configured-user",
    )

    assert result == "configured-id"


def test_get_user_falls_back_to_administrator():
    """指定用户和配置用户均不存在时应回退管理员。"""
    result = _resolve_user(
        users=[
            {"Id": "regular-id", "Name": "regular", "Policy": {}},
            {"Id": "admin-id", "Name": "admin", "Policy": {"IsAdministrator": True}},
        ],
        requested_username="missing-mp-user",
        configured_username="missing-configured-user",
    )

    assert result == "admin-id"
