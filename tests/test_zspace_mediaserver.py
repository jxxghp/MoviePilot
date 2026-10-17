"""
验证极空间认证、用户回退及 Emby 兼容接口。
"""

from unittest.mock import Mock, patch

from app.modules.zspace.zspace import ZSpace


class _FakeResponse:
    """
    保存预设接口响应并提供 JSON 读取能力。
    """

    def __init__(self, payload: dict | list):
        """
        保存模拟接口的响应内容与状态，避免实际网络请求。
        """
        self._payload = payload

    def json(self):
        """
        返回预设响应正文，不发起实际请求。
        """
        return self._payload


def test_reconnect_uses_username_password_login() -> None:
    """
    重新连接极空间时使用配置的用户名和密码登录。
    """
    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils = request_utils_cls.return_value
        request_utils.post_res.return_value = _FakeResponse(
            {
                "AccessToken": "zspace-token",
                "User": {"Id": "user-id"},
            }
        )
        request_utils.get_res.side_effect = [
            _FakeResponse([]),
            _FakeResponse({"Id": "server-id"}),
        ]

        client = ZSpace(
            host="http://zspace.local",
            username="admin",
            password="secret",
        )

    assert client._apikey == "zspace-token"
    assert client.user == "user-id"
    assert client.serverid == "server-id"
    assert (
        request_utils_cls.call_args_list[0].kwargs["headers"]["X-Emby-Authorization"]
        == 'MediaBrowser Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"'
    )
    assert request_utils_cls.call_args_list[1].kwargs["headers"]["X-Emby-Token"] == "zspace-token"
    assert request_utils_cls.call_args_list[1].kwargs["headers"].get("Authorization") is None
    assert request_utils.get_res.call_args_list[1].args[0] == "http://zspace.local/emby/System/Info"


def test_get_user_falls_back_to_current_login_user() -> None:
    """
    用户列表无法定位时回退到当前登录用户。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "zspace-token"
    client._username = "admin"
    client.user = "current-user-id"
    client._ZSpace__get_current_user = Mock(return_value={"Id": "current-user-id", "Name": "admin"})

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse({"invalid": True})
        user_id = client.get_user("admin")

    assert user_id == "current-user-id"
    assert request_utils_cls.return_value.get_res.call_args.args[0] == "http://zspace.local/emby/Users"
    assert request_utils_cls.call_args.kwargs["headers"]["X-Emby-Token"] == "zspace-token"


def test_authenticate_does_not_require_existing_api_key() -> None:
    """
    首次认证无需预先存在 API 密钥。
    """
    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.post_res.return_value = _FakeResponse(
            {
                "AccessToken": "user-token",
                "User": {"Id": "user-id"},
            }
        )

        client = ZSpace.__new__(ZSpace)
        client._host = "http://zspace.local/"
        client._apikey = None

        token = client.authenticate("user", "password")

    assert token == "user-token"
    headers = request_utils_cls.call_args.kwargs.get("headers") or {}
    assert (
        headers.get("X-Emby-Authorization")
        == 'MediaBrowser Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"'
    )
    assert headers.get("X-Emby-Token") is None


def test_get_resume_uses_emby_path_and_login_token_headers() -> None:
    """
    继续观看接口使用 Emby 兼容路径及登录令牌请求头。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "zspace-token"
    client.user = "user-id"
    client._sync_libraries = []
    client.get_user_library_folders = Mock(return_value=[])

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse({"Items": []})

        items = client.get_resume()

    assert items == []
    assert (
        request_utils_cls.return_value.get_res.call_args.args[0]
        == "http://zspace.local/emby/Users/user-id/Items/Resume"
    )
    assert request_utils_cls.return_value.get_res.call_args.kwargs["params"] == {
        "Limit": 100,
        "MediaTypes": "Video",
        "Fields": "ProductionYear,Path",
    }
    assert request_utils_cls.call_args.kwargs["headers"]["X-Emby-Token"] == "zspace-token"


def test_image_urls_use_emby_compatible_paths() -> None:
    """
    图片地址使用极空间支持的 Emby 兼容路径。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._playhost = "http://play.zspace.local/"
    client._apikey = "zspace-token"

    assert (
        client.get_backdrop_url("item-id", "tag-id")
        == "http://zspace.local/emby/Items/item-id/Images/Backdrop?tag=tag-id&api_key=zspace-token"
    )
    assert (
        client.get_backdrop_url("item-id", "tag-id", remote=True)
        == "http://play.zspace.local/emby/Items/item-id/Images/Backdrop?tag=tag-id&api_key=zspace-token"
    )
    assert (
        client._ZSpace__get_local_image_by_id("item-id")
        == "http://zspace.local/emby/Items/item-id/Images/Primary?api_key=zspace-token"
    )
