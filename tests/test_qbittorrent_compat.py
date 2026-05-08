import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_qbittorrent_modules():
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    core_module = types.ModuleType("app.core")
    core_module.__path__ = []
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    modules_module = types.ModuleType("app.modules")
    modules_module.__path__ = []
    qbittorrent_package_module = types.ModuleType("app.modules.qbittorrent")
    qbittorrent_package_module.__path__ = []
    log_module = types.ModuleType("app.log")
    cache_module = types.ModuleType("app.core.cache")
    config_module = types.ModuleType("app.core.config")
    metainfo_module = types.ModuleType("app.core.metainfo")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    string_module = types.ModuleType("app.utils.string")
    torrentool_module = types.ModuleType("torrentool")
    torrentool_module.__path__ = []
    torrentool_torrent_module = types.ModuleType("torrentool.torrent")
    qbittorrentapi_module = types.ModuleType("qbittorrentapi")
    qbittorrentapi_client_module = types.ModuleType("qbittorrentapi.client")
    qbittorrentapi_transfer_module = types.ModuleType("qbittorrentapi.transfer")

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warn(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    class _StringUtils:
        @staticmethod
        def get_domain_address(address, prefix=False):
            return address, 8080

        @staticmethod
        def is_magnet_link(value):
            if isinstance(value, bytes):
                return value.startswith(b"magnet:")
            return isinstance(value, str) and value.startswith("magnet:")

        @staticmethod
        def generate_random_str(_length):
            return "tmp-tag-01"

        @staticmethod
        def str_filesize(value):
            return str(value)

        @staticmethod
        def str_secends(value):
            return str(value)

    class _FileCache:
        def get(self, *_args, **_kwargs):
            return None

    class _MetaInfo:
        def __init__(self, name):
            self.name = name
            self.year = None
            self.season_episode = ""
            self.episode_list = []

    class _ModuleBase:
        pass

    class _DownloaderBase:
        def __class_getitem__(cls, _item):
            return cls

    class _Torrent:
        @staticmethod
        def from_string(content):
            return types.SimpleNamespace(name="test", total_size=len(content))

    class TorrentStatus(Enum):
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"

    class ModuleType(Enum):
        Downloader = "Downloader"

    class DownloaderType(Enum):
        Qbittorrent = "Qbittorrent"

    log_module.logger = _Logger()
    cache_module.FileCache = _FileCache
    config_module.settings = types.SimpleNamespace(TORRENT_TAG="moviepilot-tag")
    metainfo_module.MetaInfo = _MetaInfo
    schemas_module.DownloaderInfo = object
    schemas_module.TransferTorrent = object
    schemas_module.DownloadingTorrent = object
    schema_types_module.TorrentStatus = TorrentStatus
    schema_types_module.ModuleType = ModuleType
    schema_types_module.DownloaderType = DownloaderType
    string_module.StringUtils = _StringUtils
    modules_module._ModuleBase = _ModuleBase
    modules_module._DownloaderBase = _DownloaderBase
    torrentool_torrent_module.Torrent = _Torrent
    qbittorrentapi_module.TorrentDictionary = dict
    qbittorrentapi_module.TorrentFilesList = list
    qbittorrentapi_module.LoginFailed = type("LoginFailed", (Exception,), {})
    qbittorrentapi_module.Forbidden403Error = type("Forbidden403Error", (Exception,), {})
    qbittorrentapi_module.Unauthorized401Error = type("Unauthorized401Error", (Exception,), {})
    qbittorrentapi_module.Client = object
    qbittorrentapi_client_module.Client = object
    qbittorrentapi_transfer_module.TransferInfoDictionary = dict

    app_module.core = core_module
    app_module.log = log_module
    app_module.modules = modules_module
    app_module.schemas = schemas_module
    app_module.utils = utils_module
    core_module.cache = cache_module
    core_module.config = config_module
    core_module.metainfo = metainfo_module
    utils_module.string = string_module
    schemas_module.types = schema_types_module
    modules_module.qbittorrent = qbittorrent_package_module
    torrentool_module.torrent = torrentool_torrent_module

    stub_modules = {
        "app": app_module,
        "app.core": core_module,
        "app.core.cache": cache_module,
        "app.core.config": config_module,
        "app.core.metainfo": metainfo_module,
        "app.log": log_module,
        "app.modules": modules_module,
        "app.modules.qbittorrent": qbittorrent_package_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
        "app.utils": utils_module,
        "app.utils.string": string_module,
        "qbittorrentapi": qbittorrentapi_module,
        "qbittorrentapi.client": qbittorrentapi_client_module,
        "qbittorrentapi.transfer": qbittorrentapi_transfer_module,
        "torrentool": torrentool_module,
        "torrentool.torrent": torrentool_torrent_module,
    }

    for stub_module in stub_modules.values():
        stub_module._qbittorrent_test_stub = True

    qbittorrent_path = repo_root / "app" / "modules" / "qbittorrent" / "qbittorrent.py"
    qbittorrent_spec = importlib.util.spec_from_file_location(
        "app.modules.qbittorrent.qbittorrent",
        qbittorrent_path,
    )
    qbittorrent_module = importlib.util.module_from_spec(qbittorrent_spec)
    assert qbittorrent_spec and qbittorrent_spec.loader

    module_path = repo_root / "app" / "modules" / "qbittorrent" / "__init__.py"
    qbittorrent_module_spec = importlib.util.spec_from_file_location(
        "_test_qbittorrent_module",
        module_path,
    )
    module_package = importlib.util.module_from_spec(qbittorrent_module_spec)
    assert qbittorrent_module_spec and qbittorrent_module_spec.loader

    with patch.dict(sys.modules, stub_modules):
        sys.modules[qbittorrent_spec.name] = qbittorrent_module
        qbittorrent_spec.loader.exec_module(qbittorrent_module)
        qbittorrent_package_module.qbittorrent = qbittorrent_module
        qbittorrent_module_spec.loader.exec_module(module_package)

    return qbittorrent_module, module_package


qbittorrent_module, qbittorrent_package_module = _load_qbittorrent_modules()
Qbittorrent = qbittorrent_module.Qbittorrent
QbittorrentModule = qbittorrent_package_module.QbittorrentModule


class TestQbittorrentCompat(unittest.TestCase):
    def test_login_uses_api_key_header_without_auth_login(self):
        fake_client = MagicMock()
        fake_client.app_version.return_value = "v5.2.0"

        with patch.object(qbittorrent_module.qbittorrentapi, "Client", return_value=fake_client) as client_cls:
            downloader = Qbittorrent(host="http://127.0.0.1", port=8080, apikey="secret-token")

        self.assertIs(downloader.qbc, fake_client)
        fake_client.auth_log_in.assert_not_called()
        fake_client.app_version.assert_called_once_with()
        self.assertEqual(
            client_cls.call_args.kwargs["EXTRA_HEADERS"],
            {"Authorization": "Bearer secret-token"},
        )

    def test_add_torrent_accepts_structured_success_response(self):
        fake_client = MagicMock()
        fake_client.torrents_add.return_value = {
            "success_count": 1,
            "failure_count": 0,
            "pending_count": 0,
            "added_torrent_ids": ["abc123"],
        }

        with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
            downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

        success, added_torrent_ids = downloader.add_torrent(content="https://example.com/test.torrent")
        self.assertTrue(success)
        self.assertEqual(added_torrent_ids, ["abc123"])

    def test_add_torrent_accepts_pending_success_response_without_ids(self):
        fake_client = MagicMock()
        fake_client.torrents_add.return_value = {
            "success_count": 0,
            "failure_count": 0,
            "pending_count": 1,
            "added_torrent_ids": [],
        }

        with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
            downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

        success, added_torrent_ids = downloader.add_torrent(content="https://example.com/test.torrent")
        self.assertTrue(success)
        self.assertEqual(added_torrent_ids, [])

    def test_add_torrent_uses_cookie_api_for_qbittorrent_52(self):
        fake_client = MagicMock()
        fake_client.app_web_api_version.return_value = "2.11.3"
        fake_client.app_cookies.return_value = [
            {
                "domain": "old.example.com",
                "path": "/",
                "name": "old",
                "value": "cookie",
            }
        ]
        fake_client.torrents_add.return_value = "Ok."

        with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
            downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

        success, added_torrent_ids = downloader.add_torrent(
            content="https://tracker.example.com/download?id=1",
            cookie="uid=1; passkey=abc",
        )
        self.assertTrue(success)
        self.assertEqual(added_torrent_ids, [])
        set_cookie_call = fake_client.app_set_cookies.call_args.kwargs["cookies"]
        self.assertIn(
            {
                "domain": "tracker.example.com",
                "path": "/",
                "name": "uid",
                "value": "1",
            },
            set_cookie_call,
        )
        self.assertIn(
            {
                "domain": "tracker.example.com",
                "path": "/",
                "name": "passkey",
                "value": "abc",
            },
            set_cookie_call,
        )
        self.assertIsNone(fake_client.torrents_add.call_args.kwargs["cookie"])

    def test_add_torrent_keeps_legacy_cookie_param_for_old_webapi(self):
        fake_client = MagicMock()
        fake_client.app_web_api_version.return_value = "2.11.2"
        fake_client.torrents_add.return_value = "Ok."

        with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
            downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

        success, added_torrent_ids = downloader.add_torrent(
            content="https://tracker.example.com/download?id=1",
            cookie="uid=1",
        )
        self.assertTrue(success)
        self.assertEqual(added_torrent_ids, [])
        fake_client.app_set_cookies.assert_not_called()
        self.assertEqual(fake_client.torrents_add.call_args.kwargs["cookie"], "uid=1")


class TestQbittorrentModuleCompat(unittest.TestCase):
    @staticmethod
    def _build_module(server):
        module = QbittorrentModule.__new__(QbittorrentModule)
        module.get_instance = MagicMock(return_value=server)
        module.normalize_path = MagicMock(side_effect=lambda path, _downloader: path)
        module.get_default_config_name = MagicMock(return_value="default-qb")
        return module

    def test_download_prefers_added_torrent_ids_before_tag_lookup(self):
        fake_server = MagicMock()
        fake_server.add_torrent.return_value = (True, ["abc123"])
        fake_server.get_content_layout.return_value = "Original"
        fake_server.is_force_resume.return_value = False

        module = self._build_module(fake_server)
        result = module.download(
            content="magnet:?xt=urn:btih:123",
            download_dir=Path("/downloads"),
            cookie="",
            downloader="qb",
        )

        self.assertEqual(result, ("qb", "abc123", "Original", "添加下载成功"))
        fake_server.delete_torrents_tag.assert_called_once_with("abc123", "tmp-tag-01")
        fake_server.get_torrent_id_by_tag.assert_not_called()
        self.assertEqual(
            fake_server.add_torrent.call_args.kwargs["tag"],
            ["tmp-tag-01", "moviepilot-tag"],
        )

    def test_download_falls_back_to_tag_lookup_when_added_ids_missing(self):
        fake_server = MagicMock()
        fake_server.add_torrent.return_value = (True, [])
        fake_server.get_content_layout.return_value = "Original"
        fake_server.get_torrent_id_by_tag.return_value = "def456"
        fake_server.is_force_resume.return_value = False

        module = self._build_module(fake_server)
        result = module.download(
            content="magnet:?xt=urn:btih:456",
            download_dir=Path("/downloads"),
            cookie="",
            downloader="qb",
        )

        self.assertEqual(result, ("qb", "def456", "Original", "添加下载成功"))
        fake_server.delete_torrents_tag.assert_not_called()
        fake_server.get_torrent_id_by_tag.assert_called_once_with(tags="tmp-tag-01")
