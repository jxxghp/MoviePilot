import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import call, patch


def _load_jellyfin_module():
    module_name = "_test_jellyfin_module"
    if module_name in sys.modules:
        return sys.modules[module_name]

    if "app.log" not in sys.modules:
        log_module = types.ModuleType("app.log")

        class _Logger:
            def info(self, *_args, **_kwargs):
                pass

            def warning(self, *_args, **_kwargs):
                pass

            def error(self, *_args, **_kwargs):
                pass

            def debug(self, *_args, **_kwargs):
                pass

        log_module.logger = _Logger()
        sys.modules["app.log"] = log_module

    if "app.core.config" not in sys.modules:
        config_module = types.ModuleType("app.core.config")
        config_module.settings = types.SimpleNamespace(SUPERUSER="admin", USER_AGENT="MoviePilot")
        sys.modules["app.core.config"] = config_module

    if "app.schemas" not in sys.modules:
        schemas_module = types.ModuleType("app.schemas")
        schemas_module.MediaType = types.SimpleNamespace(MOVIE=types.SimpleNamespace(value="movie"))
        schemas_module.MediaServerItem = object
        schemas_module.MediaServerLibrary = object
        schemas_module.Statistic = object
        schemas_module.WebhookEventInfo = object
        schemas_module.MediaServerItemUserState = object
        schemas_module.MediaServerPlayItem = object
        sys.modules["app.schemas"] = schemas_module

    if "app.utils.http" not in sys.modules:
        http_module = types.ModuleType("app.utils.http")

        class _RequestUtils:
            def __init__(self, *args, **kwargs):
                pass

            def get_res(self, *args, **kwargs):
                return None

        http_module.RequestUtils = _RequestUtils
        sys.modules["app.utils.http"] = http_module

    if "app.utils.url" not in sys.modules:
        url_module = types.ModuleType("app.utils.url")

        class _UrlUtils:
            @staticmethod
            def standardize_base_url(host):
                if not host:
                    return host
                if not host.endswith("/"):
                    host += "/"
                if not host.startswith("http://") and not host.startswith("https://"):
                    host = "http://" + host
                return host

            @staticmethod
            def combine_url(host, path=None, query=None):
                from urllib.parse import urljoin

                if path is None:
                    path = "/"
                host = _UrlUtils.standardize_base_url(host)
                return urljoin(host, path)

        url_module.UrlUtils = _UrlUtils
        sys.modules["app.utils.url"] = url_module

    jellyfin_path = Path(__file__).resolve().parents[1] / "app" / "modules" / "jellyfin" / "jellyfin.py"
    spec = importlib.util.spec_from_file_location(module_name, jellyfin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


jellyfin_module = _load_jellyfin_module()
Jellyfin = jellyfin_module.Jellyfin


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class JellyfinUserResolutionTest(unittest.TestCase):
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

        with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls, patch.object(
            jellyfin_module.logger, "warning"
        ) as warning_mock:
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
                "Policy": {"IsAdministrator": True, "EnabledFolders": ["lib-1", "lib-2", "lib-3"]},
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
                "Policy": {"IsAdministrator": True, "EnabledFolders": ["lib-1", "lib-2", "lib-3"]},
            },
            {"Id": "user-id", "Name": "normal", "Policy": {"IsAdministrator": False}},
        ]

        with patch.object(jellyfin_module, "RequestUtils") as request_utils_cls, patch.object(
            jellyfin_module.logger, "warning"
        ) as warning_mock:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse(payload)

            user_id = client.get_user("admin")

        self.assertEqual(user_id, "large-admin-id")
        self.assertGreaterEqual(warning_mock.call_count, 2)

        warning_messages = [
            call.args[0] for call in warning_mock.call_args_list if call.args and isinstance(call.args[0], str)
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
        self.assertTrue(any(("回退" in message) or ("fallback" in message.lower()) for message in warning_messages))

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
            request_utils_cls.return_value.get_res.return_value = _FakeResponse({"Items": []})

            libraries = client._Jellyfin__get_jellyfin_librarys()

        self.assertEqual(libraries, [])
        request_utils_cls.return_value.get_res.assert_called_once_with(
            "http://jellyfin.local:8096/Users/user-id/Views",
            {"api_key": "api-key"},
        )


if __name__ == "__main__":
    unittest.main()
