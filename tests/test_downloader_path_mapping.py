import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _load_downloader_base():
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    helper_module = types.ModuleType("app.helper")
    helper_module.__path__ = []
    service_module = types.ModuleType("app.helper.service")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    mixins_module = types.ModuleType("app.utils.mixins")

    class StorageSchema(Enum):
        Local = "local"
        Rclone = "rclone"

    class _ConfigReloadMixin:
        pass

    class _ServiceConfigHelper:
        @staticmethod
        def get_downloader_configs():
            return []

        @staticmethod
        def get_notification_configs():
            return []

        @staticmethod
        def get_mediaserver_configs():
            return []

    schema_types_module.StorageSchema = StorageSchema
    schema_types_module.ModuleType = Enum("ModuleType", {"Downloader": "downloader"})
    schema_types_module.DownloaderType = Enum("DownloaderType", {"Qbittorrent": "Qbittorrent"})
    schema_types_module.MediaServerType = Enum("MediaServerType", {"Emby": "Emby"})
    schema_types_module.MessageChannel = Enum("MessageChannel", {"Telegram": "telegram"})
    schema_types_module.OtherModulesType = Enum("OtherModulesType", {"Subtitle": "subtitle"})
    schema_types_module.SystemConfigKey = Enum(
        "SystemConfigKey",
        {
            "Downloaders": "Downloaders",
            "Notifications": "Notifications",
            "MediaServers": "MediaServers",
        },
    )

    service_module.ServiceConfigHelper = _ServiceConfigHelper
    mixins_module.ConfigReloadMixin = _ConfigReloadMixin
    schemas_module.Notification = object
    schemas_module.NotificationConf = object
    schemas_module.MediaServerConf = object
    schemas_module.DownloaderConf = object

    app_module.helper = helper_module
    app_module.schemas = schemas_module
    app_module.utils = utils_module
    helper_module.service = service_module
    schemas_module.types = schema_types_module
    utils_module.mixins = mixins_module

    stub_modules = {
        "app": app_module,
        "app.helper": helper_module,
        "app.helper.service": service_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
        "app.utils": utils_module,
        "app.utils.mixins": mixins_module,
    }

    module_path = repo_root / "app" / "modules" / "__init__.py"
    module_spec = __import__("importlib.util").util.spec_from_file_location(
        "_test_downloader_base_module",
        module_path,
    )
    module = __import__("importlib.util").util.module_from_spec(module_spec)
    assert module_spec and module_spec.loader
    with patch.dict(sys.modules, stub_modules):
        module_spec.loader.exec_module(module)
    return module._DownloaderBase


def _load_transmission_module():
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    core_module = types.ModuleType("app.core")
    core_module.__path__ = []
    cache_module = types.ModuleType("app.core.cache")
    modules_module = types.ModuleType("app.modules")
    modules_module.__path__ = []
    transmission_package_module = types.ModuleType("app.modules.transmission")
    transmission_package_module.__path__ = []
    transmission_client_module = types.ModuleType("app.modules.transmission.transmission")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    config_module = types.ModuleType("app.core.config")
    metainfo_module = types.ModuleType("app.core.metainfo")
    log_module = types.ModuleType("app.log")
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    string_module = types.ModuleType("app.utils.string")
    transmission_rpc_module = types.ModuleType("transmission_rpc")
    torrentool_module = types.ModuleType("torrentool")
    torrentool_module.__path__ = []
    torrentool_torrent_module = types.ModuleType("torrentool.torrent")

    class _ModuleBase:
        pass

    class _DownloaderBase:
        def __class_getitem__(cls, _item):
            return cls

    class _TransferTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _DownloadingTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TorrentStatus(Enum):
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"

    class _Logger:
        def debug(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    class _MetaInfo:
        def __init__(self, name):
            self.name = name
            self.year = None
            self.season_episode = ""
            self.episode_list = []

    class _StringUtils:
        @staticmethod
        def is_magnet_link(value):
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

    transmission_client_module.Transmission = object
    cache_module.FileCache = _FileCache
    schemas_module.TransferTorrent = _TransferTorrent
    schemas_module.DownloadingTorrent = _DownloadingTorrent
    schemas_module.DownloaderInfo = object
    schema_types_module.TorrentStatus = TorrentStatus
    schema_types_module.ModuleType = Enum("ModuleType", {"Downloader": "downloader"})
    schema_types_module.DownloaderType = Enum(
        "DownloaderType", {"Transmission": "Transmission"}
    )
    config_module.settings = SimpleNamespace(TORRENT_TAG="moviepilot-tag")
    metainfo_module.MetaInfo = _MetaInfo
    log_module.logger = _Logger()
    modules_module._ModuleBase = _ModuleBase
    modules_module._DownloaderBase = _DownloaderBase
    string_module.StringUtils = _StringUtils
    transmission_rpc_module.File = object
    torrentool_torrent_module.Torrent = SimpleNamespace(
        from_string=lambda _content: SimpleNamespace(name="test", total_size=1)
    )

    app_module.core = core_module
    app_module.modules = modules_module
    app_module.schemas = schemas_module
    app_module.utils = utils_module
    core_module.cache = cache_module
    core_module.config = config_module
    core_module.metainfo = metainfo_module
    modules_module.transmission = transmission_package_module
    transmission_package_module.transmission = transmission_client_module
    schemas_module.types = schema_types_module
    utils_module.string = string_module
    torrentool_module.torrent = torrentool_torrent_module

    stub_modules = {
        "app": app_module,
        "app.core": core_module,
        "app.core.cache": cache_module,
        "app.core.config": config_module,
        "app.core.metainfo": metainfo_module,
        "app.log": log_module,
        "app.modules": modules_module,
        "app.modules.transmission": transmission_package_module,
        "app.modules.transmission.transmission": transmission_client_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
        "app.utils": utils_module,
        "app.utils.string": string_module,
        "transmission_rpc": transmission_rpc_module,
        "torrentool": torrentool_module,
        "torrentool.torrent": torrentool_torrent_module,
    }

    module_path = repo_root / "app" / "modules" / "transmission" / "__init__.py"
    module_spec = __import__("importlib.util").util.spec_from_file_location(
        "_test_transmission_module",
        module_path,
    )
    module = __import__("importlib.util").util.module_from_spec(module_spec)
    assert module_spec and module_spec.loader
    with patch.dict(sys.modules, stub_modules):
        module_spec.loader.exec_module(module)
    return module.TransmissionModule, TorrentStatus


DownloaderBase = _load_downloader_base()
TransmissionModule, TransmissionTorrentStatus = _load_transmission_module()


class DownloaderPathMappingTest(unittest.TestCase):
    def _build_base(self, path_mapping):
        downloader = DownloaderBase.__new__(DownloaderBase)
        downloader.get_config = MagicMock(
            return_value=SimpleNamespace(path_mapping=path_mapping)
        )
        return downloader

    def test_normalize_path_maps_moviepilot_path_to_downloader_path(self):
        downloader = self._build_base(
            [("/media", "/mnt/raid5/home_lt999lt")]
        )

        result = downloader.normalize_path(
            Path("/media/video/downloads/movie"), "tr"
        )

        self.assertEqual(result, "/mnt/raid5/home_lt999lt/video/downloads/movie")

    def test_normalize_return_path_maps_downloader_path_back_to_moviepilot_path(self):
        downloader = self._build_base(
            [("/media", "/mnt/raid5/home_lt999lt")]
        )

        result = downloader.normalize_return_path(
            Path("/mnt/raid5/home_lt999lt/video/downloads/TV/Show.mkv"), "tr"
        )

        self.assertEqual(result, "/media/video/downloads/TV/Show.mkv")

    def test_path_mapping_matches_complete_path_segment_only(self):
        downloader = self._build_base([("/media", "/mnt/media")])

        result = downloader.normalize_return_path(
            Path("/mnt/media2/Show.mkv"), "tr"
        )

        self.assertEqual(result, "/mnt/media2/Show.mkv")

    def test_blank_path_mapping_entry_is_ignored(self):
        downloader = self._build_base(
            [("", "/downloads"), ("/media2", ""), ("/media", "/mnt/media")]
        )

        result = downloader.normalize_return_path(Path("/mnt/media/Show.mkv"), "tr")

        self.assertEqual(result, "/media/Show.mkv")

    def test_normalize_path_strips_storage_prefix_after_mapping(self):
        downloader = self._build_base([("local:/media", "/downloads")])

        result = downloader.normalize_path(Path("local:/media/movie"), "qb")

        self.assertEqual(result, "/downloads/movie")


class TransmissionPathMappingTest(unittest.TestCase):
    def _build_module(self, server):
        module = TransmissionModule.__new__(TransmissionModule)
        module.get_instances = MagicMock(return_value={"tr": server})
        module.get_instance = MagicMock(return_value=server)
        module.normalize_return_path = MagicMock(
            side_effect=lambda path, _downloader: str(path).replace(
                "/mnt/raid5/home_lt999lt", "/media", 1
            )
        )
        return module

    def test_completed_torrents_return_moviepilot_accessible_path(self):
        server = MagicMock()
        server.get_completed_torrents.return_value = [
            SimpleNamespace(
                name="Show.S01E01.mkv",
                download_dir="/mnt/raid5/home_lt999lt/video/downloads/TV",
                hashString="hash-tr",
                labels=[],
                progress=100,
                status="seeding",
            )
        ]
        module = self._build_module(server)

        torrents = module.list_torrents(status=TransmissionTorrentStatus.TRANSFER)

        self.assertEqual(torrents[0].path, Path("/media/video/downloads/TV/Show.S01E01.mkv"))
        module.normalize_return_path.assert_called_once_with(
            Path("/mnt/raid5/home_lt999lt/video/downloads/TV/Show.S01E01.mkv"),
            "tr",
        )

    def test_hash_lookup_return_moviepilot_accessible_path(self):
        server = MagicMock()
        server.get_torrents.return_value = (
            [
                SimpleNamespace(
                    name="Movie",
                    download_dir="/mnt/raid5/home_lt999lt/video/downloads/movie",
                    hashString="hash-tr",
                    total_size=1024,
                    labels=[],
                    progress=100,
                    status="seeding",
                )
            ],
            False,
        )
        module = self._build_module(server)

        torrents = module.list_torrents(hashs=["hash-tr"], downloader="tr")

        self.assertEqual(torrents[0].path, Path("/media/video/downloads/movie/Movie"))
        module.normalize_return_path.assert_called_once_with(
            Path("/mnt/raid5/home_lt999lt/video/downloads/movie/Movie"),
            "tr",
        )
