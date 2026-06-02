import unittest
from unittest.mock import Mock, patch

from app.modules.zspace.zspace import ZSpace


class _FakeResponse:
    def __init__(self, payload: dict | list):
        self._payload = payload

    def json(self):
        return self._payload


class ZSpaceMediaServerTest(unittest.TestCase):
    def test_reconnect_uses_username_password_login(self):
        with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
            request_utils = request_utils_cls.return_value
            request_utils.post_res.return_value = _FakeResponse({
                "AccessToken": "zspace-token",
                "User": {"Id": "user-id"},
            })
            request_utils.get_res.side_effect = [
                _FakeResponse([]),
                _FakeResponse({"Id": "server-id"}),
            ]

            client = ZSpace(
                host="http://zspace.local",
                username="admin",
                password="secret",
            )

        self.assertEqual(client._apikey, "zspace-token")
        self.assertEqual(client.user, "user-id")
        self.assertEqual(client.serverid, "server-id")
        self.assertEqual(
            request_utils_cls.call_args_list[0].kwargs["headers"]["X-Emby-Authorization"],
            'MediaBrowser Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"',
        )
        self.assertEqual(
            request_utils_cls.call_args_list[1].kwargs["headers"]["X-Emby-Token"],
            "zspace-token",
        )
        self.assertIsNone(
            request_utils_cls.call_args_list[1].kwargs["headers"].get("Authorization")
        )
        self.assertEqual(
            request_utils.get_res.call_args_list[1].args[0],
            "http://zspace.local/emby/System/Info",
        )

    def test_get_user_falls_back_to_current_login_user(self):
        client = ZSpace.__new__(ZSpace)
        client._host = "http://zspace.local/"
        client._apikey = "zspace-token"
        client._username = "admin"
        client.user = "current-user-id"
        client._ZSpace__get_current_user = Mock(return_value={"Id": "current-user-id", "Name": "admin"})

        with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({"invalid": True})
            user_id = client.get_user("admin")

        self.assertEqual(user_id, "current-user-id")
        self.assertEqual(
            request_utils_cls.return_value.get_res.call_args.args[0],
            "http://zspace.local/emby/Users",
        )
        self.assertEqual(
            request_utils_cls.call_args.kwargs["headers"]["X-Emby-Token"],
            "zspace-token",
        )

    def test_authenticate_does_not_require_existing_api_key(self):
        with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.post_res.return_value = _FakeResponse({
                "AccessToken": "user-token",
                "User": {"Id": "user-id"},
            })

            client = ZSpace.__new__(ZSpace)
            client._host = "http://zspace.local/"
            client._apikey = None

            token = client.authenticate("user", "password")

        self.assertEqual(token, "user-token")
        headers = request_utils_cls.call_args.kwargs.get("headers") or {}
        self.assertEqual(
            headers.get("X-Emby-Authorization"),
            'MediaBrowser Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"',
        )
        self.assertIsNone(headers.get("X-Emby-Token"))

    def test_get_resume_uses_emby_path_and_login_token_headers(self):
        client = ZSpace.__new__(ZSpace)
        client._host = "http://zspace.local/"
        client._apikey = "zspace-token"
        client.user = "user-id"
        client._sync_libraries = []
        client.get_user_library_folders = Mock(return_value=[])

        with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({"Items": []})

            items = client.get_resume()

        self.assertEqual(items, [])
        self.assertEqual(
            request_utils_cls.return_value.get_res.call_args.args[0],
            "http://zspace.local/emby/Users/user-id/Items/Resume",
        )
        self.assertEqual(
            request_utils_cls.return_value.get_res.call_args.kwargs["params"],
            {
                "Limit": 100,
                "MediaTypes": "Video",
                "Fields": "ProductionYear,Path",
            },
        )
        self.assertEqual(
            request_utils_cls.call_args.kwargs["headers"]["X-Emby-Token"],
            "zspace-token",
        )

    def test_image_urls_use_emby_compatible_paths(self):
        client = ZSpace.__new__(ZSpace)
        client._host = "http://zspace.local/"
        client._playhost = "http://play.zspace.local/"
        client._apikey = "zspace-token"

        self.assertEqual(
            client.get_backdrop_url("item-id", "tag-id"),
            "http://zspace.local/emby/Items/item-id/Images/Backdrop?tag=tag-id&api_key=zspace-token",
        )
        self.assertEqual(
            client.get_backdrop_url("item-id", "tag-id", remote=True),
            "http://play.zspace.local/emby/Items/item-id/Images/Backdrop?tag=tag-id&api_key=zspace-token",
        )
        self.assertEqual(
            client._ZSpace__get_local_image_by_id("item-id"),
            "http://zspace.local/emby/Items/item-id/Images/Primary?api_key=zspace-token",
        )
