import sys
import unittest
from unittest.mock import patch

from app.modules.jellyfin import jellyfin as jellyfin_module
from app.modules.jellyfin.jellyfin import Jellyfin


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class JellyfinUserResolutionTest(unittest.TestCase):
    def test_loader_does_not_leave_stub_modules_in_sys_modules(self):
        self.assertNotIn("_test_jellyfin_module", sys.modules)
        self.assertFalse(
            getattr(sys.modules.get("app.log"), "_jellyfin_test_stub", False)
        )
        self.assertFalse(
            getattr(sys.modules.get("app.core.config"), "_jellyfin_test_stub", False)
        )
        self.assertFalse(
            getattr(sys.modules.get("app.utils.http"), "_jellyfin_test_stub", False)
        )

    def _build_client(self) -> Jellyfin:
        client = Jellyfin.__new__(Jellyfin)
        client._host = "http://jellyfin.local:8096"
        client._apikey = "api-key"
        client._playhost = None
        client._sync_libraries = []
        client.user = "fallback-user"
        return client

    def test_get_user_prefers_exact_username_without_warning(self):
        client = self._build_client()
        payload = [
            {"Id": "admin-id", "Name": "admin", "Policy": {"IsAdministrator": True}},
            {"Id": "alice-id", "Name": "alice", "Policy": {"IsAdministrator": False}},
        ]

        with (
            patch.object(jellyfin_module, "RequestUtils") as request_utils_cls,
            patch.object(jellyfin_module.logger, "warning") as warning_mock,
        ):
            request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

            user_id = client.get_user("alice")

        self.assertEqual(user_id, "alice-id")
        warning_mock.assert_not_called()

    def test_get_user_prefers_enable_all_folders_admin(self):
        client = self._build_client()
        payload = [
            {
                "Id": "visible-admin-id",
                "Name": "visible",
                "Policy": {
                    "IsAdministrator": True,
                    "EnabledFolders": ["lib-1", "lib-2", "lib-3"],
                },
            },
            {
                "Id": "full-admin-id",
                "Name": "full",
                "Policy": {"IsAdministrator": True, "EnableAllFolders": True},
            },
        ]

        with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

            user_id = client.get_user()

        self.assertEqual(user_id, "full-admin-id")

    def test_get_user_warns_and_prefers_larger_visible_scope_admin(self):
        client = self._build_client()
        payload = [
            {
                "Id": "small-admin-id",
                "Name": "small",
                "Policy": {"IsAdministrator": True, "EnabledFolders": ["lib-1"]},
            },
            {
                "Id": "large-admin-id",
                "Name": "large",
                "Policy": {
                    "IsAdministrator": True,
                    "EnabledFolders": ["lib-1", "lib-2", "lib-3"],
                },
            },
            {"Id": "user-id", "Name": "normal", "Policy": {"IsAdministrator": False}},
        ]

        with (
            patch.object(jellyfin_module, "RequestUtils") as request_utils_cls,
            patch.object(jellyfin_module.logger, "warning") as warning_mock,
        ):
            request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

            user_id = client.get_user("admin")

        self.assertEqual(user_id, "large-admin-id")
        self.assertGreaterEqual(warning_mock.call_count, 2)

        warning_messages = [
            call.args[0]
            for call in warning_mock.call_args_list
            if call.args and isinstance(call.args[0], str)
        ]
        self.assertTrue(any("超级管理员" in message for message in warning_messages))
        self.assertTrue(
            any(
                ("部分" in message)
                or ("可见" in message)
                or ("访问范围" in message)
                or ("EnabledFolders" in message)
                for message in warning_messages
            )
        )
        self.assertTrue(
            any(
                ("回退" in message) or ("fallback" in message.lower())
                for message in warning_messages
            )
        )

    def test_get_jellyfin_librarys_returns_empty_when_user_missing(self):
        client = self._build_client()
        client.user = None

        with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
            libraries = client._Jellyfin__get_jellyfin_librarys()

        self.assertEqual(libraries, [])
        request_utils_cls.assert_not_called()

    def test_get_jellyfin_librarys_uses_normalized_views_url(self):
        client = self._build_client()
        client._host = "http://jellyfin.local:8096"
        client.user = "user-id"

        with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse(
                {"Items": []}
            )

            libraries = client._Jellyfin__get_jellyfin_librarys()

        self.assertEqual(libraries, [])
        request_utils_cls.return_value.get_res.assert_called_once_with(
            "http://jellyfin.local:8096/Users/user-id/Views",
            {"api_key": "api-key"},
        )
