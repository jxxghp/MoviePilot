import unittest
from unittest.mock import patch
import importlib.util
import sys
import types
from pathlib import Path

from app import schemas

try:
    from app.api.endpoints import dashboard as dashboard_endpoint
except Exception:
    dashboard_endpoint = None


def _load_ugreen_class():
    """
    在测试中动态加载 Ugreen，避免受可选依赖（如 pyquery/sqlalchemy）影响。
    """
    module_name = "_test_ugreen_module"
    if module_name in sys.modules:
        return sys.modules[module_name].Ugreen

    # 轻量日志桩
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

    # SystemConfigOper 桩
    if "app.db.systemconfig_oper" not in sys.modules:
        db_module = types.ModuleType("app.db.systemconfig_oper")

        class _SystemConfigOper:
            @staticmethod
            def get(_key):
                return {}

            @staticmethod
            def set(_key, _value):
                return None

        db_module.SystemConfigOper = _SystemConfigOper
        sys.modules["app.db.systemconfig_oper"] = db_module

    # app.modules / app.modules.ugreen / app.modules.ugreen.api 桩
    if "app.modules" not in sys.modules:
        pkg = types.ModuleType("app.modules")
        pkg.__path__ = []
        sys.modules["app.modules"] = pkg
    if "app.modules.ugreen" not in sys.modules:
        subpkg = types.ModuleType("app.modules.ugreen")
        subpkg.__path__ = []
        sys.modules["app.modules.ugreen"] = subpkg
    if "app.modules.ugreen.api" not in sys.modules:
        api_module = types.ModuleType("app.modules.ugreen.api")

        class _Api:
            host = ""
            token = None

        api_module.Api = _Api
        sys.modules["app.modules.ugreen.api"] = api_module

    ugreen_path = Path(__file__).resolve().parents[1] / "app" / "modules" / "ugreen" / "ugreen.py"
    spec = importlib.util.spec_from_file_location(module_name, ugreen_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.Ugreen


Ugreen = _load_ugreen_class()


class _FakeUgreenApi:
    host = "http://127.0.0.1:9999"
    token = "test-token"

    @staticmethod
    def video_all(classification: int, page: int = 1, page_size: int = 1):
        if classification == -102:
            return {"total_num": 12}
        if classification == -103:
            return {"total_num": 34}
        return {"total_num": 0}


class _FakeReconnectApi:
    token = "test-token"

    @staticmethod
    def login(_username, _password):
        return "test-token"

    @staticmethod
    def current_user():
        return {"name": "tester"}

    @staticmethod
    def close():
        return None

    @staticmethod
    def export_session_state():
        return {"token": "test-token", "public_key": "public-key"}


class _PagedFolderApi:
    def __init__(self, stop_after: int | None = None):
        self.calls = 0
        self.pages = []
        self.stop_after = stop_after

    def poster_wall_get_folder(self, page: int, page_size: int = 100):
        self.calls += 1
        self.pages.append(page)
        if self.stop_after is not None and page >= self.stop_after:
            return {
                "folder_arr": [
                    {"media_lib_set_id": page, "path": f"/library/{page}"},
                ],
                "is_last_page": True,
            }
        return {
            "folder_arr": [
                {"media_lib_set_id": page, "path": f"/library/{page}"},
            ],
            "is_last_page": False,
        }


class UgreenScanModeTest(unittest.TestCase):
    def test_resolve_scan_type(self):
        resolve = Ugreen._Ugreen__resolve_scan_type

        self.assertEqual(resolve(scan_mode="new_and_modified"), 1)
        self.assertEqual(resolve(scan_mode="supplement_missing"), 2)
        self.assertEqual(resolve(scan_mode="full_override"), 3)

        self.assertEqual(resolve(scan_mode="1"), 1)
        self.assertEqual(resolve(scan_mode="2"), 2)
        self.assertEqual(resolve(scan_mode="3"), 3)

        self.assertEqual(resolve(scan_type=1), 1)
        self.assertEqual(resolve(scan_type=2), 2)
        self.assertEqual(resolve(scan_type=3), 3)

        self.assertEqual(resolve(scan_mode="unknown"), 2)
        self.assertEqual(resolve(), 2)


class UgreenVerifySslTest(unittest.TestCase):
    def test_resolve_verify_ssl(self):
        resolve = Ugreen._Ugreen__resolve_verify_ssl
        self.assertEqual(resolve(True), True)
        self.assertEqual(resolve(False), False)
        self.assertEqual(resolve("true"), True)
        self.assertEqual(resolve("1"), True)
        self.assertEqual(resolve("false"), False)
        self.assertEqual(resolve("0"), False)
        self.assertEqual(resolve(None), True)


class UgreenStatisticTest(unittest.TestCase):
    def test_get_medias_count_episode_is_none(self):
        ugreen = Ugreen.__new__(Ugreen)
        ugreen._host = "http://127.0.0.1:9999"
        ugreen._username = "tester"
        ugreen._password = "secret"
        ugreen._userinfo = {"name": "tester"}
        ugreen._api = _FakeUgreenApi()

        stat = ugreen.get_medias_count()
        self.assertEqual(stat.movie_count, 12)
        self.assertEqual(stat.tv_count, 34)
        self.assertIsNone(stat.episode_count)


class UgreenReconnectTest(unittest.TestCase):
    def test_reconnect_does_not_eagerly_load_libraries(self):
        ugreen = Ugreen.__new__(Ugreen)
        ugreen._host = "http://127.0.0.1:9999"
        ugreen._username = "tester"
        ugreen._password = "secret"
        ugreen._verify_ssl = True
        ugreen._libraries = {"old": {"id": "old"}}
        ugreen._library_paths = {"old": "/old"}
        ugreen._api = None
        ugreen._userinfo = None

        with patch.object(Ugreen, "_Ugreen__restore_persisted_session", return_value=False), patch(
            "_test_ugreen_module.Api", return_value=_FakeReconnectApi()
        ), patch.object(Ugreen, "_Ugreen__save_persisted_session", return_value=None), patch.object(
            Ugreen, "disconnect", wraps=ugreen.disconnect
        ), patch.object(Ugreen, "get_librarys") as mocked_get_librarys:
            self.assertTrue(ugreen.reconnect())

        mocked_get_librarys.assert_not_called()
        self.assertEqual(ugreen._libraries, {})
        self.assertEqual(ugreen._library_paths, {})


class UgreenLibraryPathLimitTest(unittest.TestCase):
    def test_load_library_paths_stops_at_last_page(self):
        ugreen = Ugreen.__new__(Ugreen)
        ugreen._username = "tester"
        ugreen._api = _PagedFolderApi(stop_after=3)

        paths = ugreen._Ugreen__load_library_paths()

        self.assertEqual(ugreen._api.pages, [1, 2, 3])
        self.assertEqual(paths["3"], "/library/3")

    def test_load_library_paths_respects_page_limit(self):
        ugreen = Ugreen.__new__(Ugreen)
        ugreen._username = "tester"
        ugreen._api = _PagedFolderApi()

        paths = ugreen._Ugreen__load_library_paths()

        self.assertEqual(ugreen._api.calls, Ugreen.LIBRARY_PATH_PAGE_LIMIT)
        self.assertEqual(len(paths), Ugreen.LIBRARY_PATH_PAGE_LIMIT)
        self.assertIn(str(Ugreen.LIBRARY_PATH_PAGE_LIMIT), paths)


class DashboardStatisticTest(unittest.TestCase):
    @unittest.skipIf(dashboard_endpoint is None, "dashboard endpoint dependencies are missing")
    def test_statistic_all_episode_missing(self):
        mocked_stats = [
            schemas.Statistic(movie_count=10, tv_count=20, episode_count=None, user_count=2),
            schemas.Statistic(movie_count=1, tv_count=2, episode_count=None, user_count=1),
        ]
        with patch(
            "app.api.endpoints.dashboard.DashboardChain.media_statistic",
            return_value=mocked_stats,
        ):
            ret = dashboard_endpoint.statistic(name="ugreen", _=None)

        self.assertEqual(ret.movie_count, 11)
        self.assertEqual(ret.tv_count, 22)
        self.assertEqual(ret.user_count, 3)
        self.assertIsNone(ret.episode_count)

    @unittest.skipIf(dashboard_endpoint is None, "dashboard endpoint dependencies are missing")
    def test_statistic_mixed_episode_count(self):
        mocked_stats = [
            schemas.Statistic(movie_count=10, tv_count=20, episode_count=None, user_count=2),
            schemas.Statistic(movie_count=1, tv_count=2, episode_count=6, user_count=1),
        ]
        with patch(
            "app.api.endpoints.dashboard.DashboardChain.media_statistic",
            return_value=mocked_stats,
        ):
            ret = dashboard_endpoint.statistic(name="all", _=None)

        self.assertEqual(ret.movie_count, 11)
        self.assertEqual(ret.tv_count, 22)
        self.assertEqual(ret.user_count, 3)
        self.assertEqual(ret.episode_count, 6)


if __name__ == "__main__":
    unittest.main()
