from unittest.mock import patch

from app.modules.jellyfin.jellyfin import Jellyfin


class _FakeResponse:
    """模拟 Jellyfin HTTP 响应。"""

    def __init__(self, payload: object):
        """保存响应数据。"""
        self._payload = payload

    def json(self) -> object:
        """返回模拟的 JSON 数据。"""
        return self._payload


def _make_client() -> Jellyfin:
    """构造跳过初始化的 Jellyfin 客户端。"""
    client = Jellyfin.__new__(Jellyfin)
    client._host = "http://jellyfin.local:8096/"
    client._apikey = "api-key"
    client._playhost = None
    client._sync_libraries = []
    client.user = "user-id"
    return client


def test_get_user_supports_legacy_query_and_jellyfin_12_header():
    """用户查询应同时兼容旧版查询参数与 Jellyfin 12 请求头鉴权。"""
    client = _make_client()

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            [{"Id": "user-id", "Name": "admin"}]
        )

        user_id = client.get_user("admin")

    assert user_id == "user-id"
    assert request_utils_cls.call_args.kwargs["headers"] == {
        "Authorization": 'MediaBrowser Token="api-key"'
    }
    request_utils_cls.return_value.get_res.assert_called_once_with(
        "http://jellyfin.local:8096/Users",
        {"api_key": "api-key"},
    )


def test_authenticate_preserves_client_headers_and_adds_jellyfin_12_header():
    """用户认证应保留客户端声明并补充 Jellyfin 12 请求头鉴权。"""
    client = _make_client()

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.post_res.return_value = _FakeResponse(
            {"AccessToken": "user-token"}
        )

        token = client.authenticate("admin", "password")

    assert token == "user-token"
    headers = request_utils_cls.call_args.kwargs["headers"]
    assert headers["Authorization"] == 'MediaBrowser Token="api-key"'
    assert headers["X-Emby-Authorization"].endswith('Token="api-key"')
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"


def test_post_data_preserves_explicit_authorization_header():
    """自定义请求显式提供 Authorization 时不应被服务器密钥覆盖。"""
    client = _make_client()

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        client.post_data(
            "[HOST]Sessions/Playing",
            headers={"Authorization": "Custom token", "X-Test": "value"},
        )

    assert request_utils_cls.call_args.kwargs["headers"] == {
        "Authorization": "Custom token",
        "X-Test": "value",
    }
