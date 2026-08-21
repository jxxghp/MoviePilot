import sys
import types
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
    service_module = types.ModuleType("app.runtime.extensions.service_config")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    mixins_module = types.ModuleType("app.runtime.reload")

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

    def _select_instance_configs(configs, service_type, *, multi_instance=True):
        """按类型标识与启用态筛出实例配置的替身。

        :param configs: 该族全部配置
        :param service_type: 类型标识
        :param multi_instance: 该类型能否接受多份配置，本替身不区分
        :return: 实例名到配置的映射
        """
        del multi_instance
        if not service_type:
            return {}
        return {
            conf.name: conf
            for conf in configs
            if getattr(conf, "name", None) and conf.type == service_type and conf.enabled
        }

    def _create_service_instance(name, conf, *, impl=None, factory=None):
        """按单条配置构造具名服务实例的替身。

        :param name: 实例名
        :param conf: 该实例的用户配置
        :param impl: 实例实现类
        :param factory: 实例工厂
        :return: 构造出的实例
        """
        if factory is not None:
            return factory(conf)
        return impl(name=name, **(getattr(conf, "config", None) or {}))

    def _service_capability_configs(_capability):
        """按能力标签读取配置的替身，本用例不依赖真实用户配置。

        :param _capability: 服务能力标签
        :return: 空配置列表
        """
        return []

    schema_types_module.StorageSchema = StorageSchema
    schema_types_module.ModuleType = Enum(
        "ModuleType",
        {
            "Downloader": "downloader",
            "MediaServer": "mediaserver",
            "Notification": "notification",
        },
    )
    schema_types_module.DownloaderType = Enum("DownloaderType", {"Qbittorrent": "Qbittorrent"})
    schema_types_module.MediaServerType = Enum("MediaServerType", {"Emby": "Emby"})
    schema_types_module.NotificationChannel = Enum("NotificationChannel", {"Telegram": "telegram"})
    schema_types_module.OtherModulesType = Enum("OtherModulesType", {"Subtitle": "subtitle"})
    schema_types_module.MediaRecognizeType = Enum(
        "MediaRecognizeType", {"TheMovieDb": "themoviedb"}
    )
    schema_types_module.SystemConfigKey = Enum(
        "SystemConfigKey",
        {
            "Downloaders": "Downloaders",
            "Notifications": "Notifications",
            "MediaServers": "MediaServers",
        },
    )

    service_module.ServiceConfigHelper = _ServiceConfigHelper
    service_module.select_instance_configs = _select_instance_configs
    service_module.create_service_instance = _create_service_instance
    service_module.service_capability_configs = _service_capability_configs
    mixins_module.ConfigReloadMixin = _ConfigReloadMixin
    schemas_module.Message = object
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
        "app.runtime.extensions.service_config": service_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
        "app.utils": utils_module,
        "app.runtime.reload": mixins_module,
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
    domain_module = types.ModuleType("app.domain")
    domain_module.__path__ = []
    foundation_module = types.ModuleType("app.foundation")
    foundation_module.__path__ = []
    torrent_rules_module = types.ModuleType("app.domain.torrent")
    size_tools_module = types.ModuleType("app.foundation.size")
    temporal_tools_module = types.ModuleType("app.foundation.temporal")
    cache_module = types.ModuleType("app.runtime.cache")
    base_module = types.ModuleType("app.modules._base")
    modules_module = types.ModuleType("app.modules")
    modules_module.__path__ = []
    transmission_package_module = types.ModuleType("app.modules.transmission")
    transmission_package_module.__path__ = []
    transmission_client_module = types.ModuleType("app.modules.transmission.transmission")
    schemas_module = types.ModuleType("app.schemas")
    schema_types_module = types.ModuleType("app.schemas.types")
    config_module = types.ModuleType("app.runtime.config")
    metainfo_module = types.ModuleType("app.domain.metainfo")
    log_module = types.ModuleType("app.runtime.log")
    transmission_rpc_module = types.ModuleType("transmission_rpc")
    torrentool_module = types.ModuleType("torrentool")
    torrentool_module.__path__ = []
    torrentool_torrent_module = types.ModuleType("torrentool.torrent")

    class _ModuleBase:
        pass

    class _DownloaderBase:
        def __class_getitem__(cls, _item):
            return cls

    class _DownloaderModuleBase(_ModuleBase, _DownloaderBase):
        """隔离测试用的下载器模块基类桩，与 app.modules._base 行为对齐。"""

        def _get_torrent_info(self, content):
            """与真实基类一致的种子信息读取，磁力链接不解析。"""
            torrent_content = content
            if isinstance(content, Path):
                torrent_content = content.read_bytes() if content.exists() else None
            torrent_info = None
            if torrent_content and not torrent_rules_module.is_magnet_link(torrent_content):
                torrent_info = torrentool_torrent_module.Torrent.from_string(
                    torrent_content
                )
            return torrent_info, torrent_content

        @staticmethod
        def _normalize_query_status(status):
            """与真实基类一致的查询状态归一，返回隔离测试枚举。"""
            status_value = getattr(status, "value", status)
            status_text = str(status_value or "").strip().lower()
            if not status_text or status_text in {"all", "全部"}:
                return TorrentQueryStatus.ALL
            if status_text in {"transfer", "transferring"}:
                return TorrentQueryStatus.TRANSFER
            if status_text in {"downloading"}:
                return TorrentQueryStatus.DOWNLOADING
            if status_text in {"complete", "completed", "seeding", "完成", "已完成"}:
                return TorrentQueryStatus.COMPLETED
            if status_text in {"pause", "paused", "暂停", "已暂停"}:
                return TorrentQueryStatus.PAUSED
            return TorrentQueryStatus.ALL

    class _TransferTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _DownloadingTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _DownloaderTorrent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TorrentStatus(Enum):
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"

    class TorrentQueryStatus(Enum):
        ALL = "all"
        TRANSFER = "transfer"
        DOWNLOADING = "downloading"
        COMPLETED = "completed"
        PAUSED = "paused"

    class DownloadTaskState(Enum):
        DOWNLOADING = "downloading"
        PAUSED = "paused"
        COMPLETED = "completed"

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

    def _is_magnet_link(value):
        """按生产领域规则识别测试磁力链接。"""
        return isinstance(value, str) and value.startswith("magnet:")

    def _format_size(value):
        """返回隔离测试需要的简化容量文本。"""
        return str(value)

    def _format_duration(value):
        """返回隔离测试需要的简化时长文本。"""
        return str(value)

    class _FileCache:
        def get(self, *_args, **_kwargs):
            return None

    transmission_client_module.Transmission = object
    cache_module.FileCache = _FileCache
    schemas_module.TransferTorrent = _TransferTorrent
    schemas_module.DownloadingTorrent = _DownloadingTorrent
    schemas_module.DownloaderTorrent = _DownloaderTorrent
    schemas_module.DownloaderInfo = object
    schema_types_module.TorrentStatus = TorrentStatus
    schema_types_module.TorrentQueryStatus = TorrentQueryStatus
    schema_types_module.DownloadTaskState = DownloadTaskState
    schema_types_module.DownloaderType = Enum(
        "DownloaderType", {"Transmission": "Transmission"}
    )
    config_module.settings = SimpleNamespace(TORRENT_TAG="moviepilot-tag")
    metainfo_module.MetaInfo = _MetaInfo
    log_module.logger = _Logger()
    modules_module._ModuleBase = _ModuleBase
    modules_module._DownloaderBase = _DownloaderBase
    modules_module._base = base_module
    base_module._DownloaderModuleBase = _DownloaderModuleBase
    torrent_rules_module.is_magnet_link = _is_magnet_link
    size_tools_module.format_compact_size = _format_size
    temporal_tools_module.format_duration = _format_duration
    transmission_rpc_module.File = object
    torrentool_torrent_module.Torrent = SimpleNamespace(
        from_string=lambda _content: SimpleNamespace(name="test", total_size=1)
    )

    app_module.core = core_module
    app_module.domain = domain_module
    app_module.foundation = foundation_module
    app_module.modules = modules_module
    app_module.schemas = schemas_module
    domain_module.torrent = torrent_rules_module
    foundation_module.size = size_tools_module
    foundation_module.temporal = temporal_tools_module
    core_module.cache = cache_module
    core_module.config = config_module
    core_module.metainfo = metainfo_module
    modules_module.transmission = transmission_package_module
    transmission_package_module.transmission = transmission_client_module
    schemas_module.types = schema_types_module
    torrentool_module.torrent = torrentool_torrent_module

    stub_modules = {
        "app": app_module,
        "app.core": core_module,
        "app.domain": domain_module,
        "app.domain.torrent": torrent_rules_module,
        "app.foundation": foundation_module,
        "app.foundation.size": size_tools_module,
        "app.foundation.temporal": temporal_tools_module,
        "app.runtime.cache": cache_module,
        "app.runtime.config": config_module,
        "app.domain.metainfo": metainfo_module,
        "app.runtime.log": log_module,
        "app.modules": modules_module,
        "app.modules._base": base_module,
        "app.modules.transmission": transmission_package_module,
        "app.modules.transmission.transmission": transmission_client_module,
        "app.schemas": schemas_module,
        "app.schemas.types": schema_types_module,
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


def _build_base(path_mapping):
    downloader = DownloaderBase.__new__(DownloaderBase)
    downloader.get_config = MagicMock(
        return_value=SimpleNamespace(path_mapping=path_mapping)
    )
    return downloader


def _build_transmission_module(server):
    module = TransmissionModule.__new__(TransmissionModule)
    module.get_instances = MagicMock(return_value={"tr": server})
    module.get_instance = MagicMock(return_value=server)
    module.normalize_return_path = MagicMock(
        side_effect=lambda path, _downloader: str(path).replace(
            "/mnt/raid5/home_lt999lt", "/media", 1
        )
    )
    return module


def test_normalize_path_maps_moviepilot_path_to_downloader_path():
    """MoviePilot 访问路径应转换为下载器容器内路径。"""
    downloader = _build_base([("/media", "/mnt/raid5/home_lt999lt")])

    result = downloader.normalize_path(Path("/media/video/downloads/movie"), "tr")

    assert result == "/mnt/raid5/home_lt999lt/video/downloads/movie"


def test_normalize_return_path_maps_downloader_path_back_to_moviepilot_path():
    """下载器容器内路径应转换回 MoviePilot 可访问路径。"""
    downloader = _build_base([("/media", "/mnt/raid5/home_lt999lt")])

    result = downloader.normalize_return_path(
        Path("/mnt/raid5/home_lt999lt/video/downloads/TV/Show.mkv"), "tr"
    )

    assert result == "/media/video/downloads/TV/Show.mkv"


def test_path_mapping_matches_complete_path_segment_only():
    """路径映射只应命中完整路径段，避免误伤相似前缀。"""
    downloader = _build_base([("/media", "/mnt/media")])

    result = downloader.normalize_return_path(Path("/mnt/media2/Show.mkv"), "tr")

    assert result == "/mnt/media2/Show.mkv"


def test_blank_path_mapping_entry_is_ignored():
    """空路径映射项应被忽略，继续使用后续有效配置。"""
    downloader = _build_base(
        [("", "/downloads"), ("/media2", ""), ("/media", "/mnt/media")]
    )

    result = downloader.normalize_return_path(Path("/mnt/media/Show.mkv"), "tr")

    assert result == "/media/Show.mkv"


def test_normalize_path_strips_storage_prefix_after_mapping():
    """带存储类型前缀的路径映射后应返回下载器原生路径。"""
    downloader = _build_base([("local:/media", "/downloads")])

    result = downloader.normalize_path(Path("local:/media/movie"), "qb")

    assert result == "/downloads/movie"


def test_completed_torrents_return_moviepilot_accessible_path():
    """Transmission 已完成任务返回的路径字段均应为 MoviePilot 可访问路径。"""
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
    module = _build_transmission_module(server)

    torrents = module.list_torrents(status=TransmissionTorrentStatus.TRANSFER)

    assert torrents[0].path == Path("/media/video/downloads/TV/Show.S01E01.mkv")
    assert torrents[0].save_path == "/media/video/downloads/TV"
    assert torrents[0].content_path == "/media/video/downloads/TV/Show.S01E01.mkv"


def test_hash_lookup_return_moviepilot_accessible_path():
    """Transmission 按 Hash 查询时返回的路径字段均应完成路径映射。"""
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
    module = _build_transmission_module(server)

    torrents = module.list_torrents(hashs=["hash-tr"], downloader="tr")

    assert torrents[0].path == Path("/media/video/downloads/movie/Movie")
    assert torrents[0].save_path == "/media/video/downloads/movie"
    assert torrents[0].content_path == "/media/video/downloads/movie/Movie"


def test_list_torrents_ignores_missing_transmission_limit_fields():
    """Transmission 任务缺少做种限制字段时不应中断列表查询。"""

    class _TorrentWithMissingLimitFields:
        """
        模拟 transmission-rpc 属性访问缺失字段时抛出 KeyError 的任务对象。
        """

        name = "Movie"
        download_dir = "/mnt/raid5/home_lt999lt/video/downloads/movie"
        hashString = "hash-missing-limit"
        total_size = 1024
        labels = []
        progress = 100
        status = "seeding"

        @property
        def seed_ratio_limit(self):
            """
            模拟 seedRatioLimit 原始字段缺失。
            """
            raise KeyError("seedRatioLimit")

        @property
        def seed_idle_limit(self):
            """
            模拟 seedIdleLimit 原始字段缺失。
            """
            raise KeyError("seedIdleLimit")

    server = MagicMock()
    server.get_torrents.return_value = (
        [_TorrentWithMissingLimitFields()],
        False,
    )
    module = _build_transmission_module(server)

    torrents = module.list_torrents()

    assert torrents[0].hash == "hash-missing-limit"
    assert torrents[0].ratio_limit is None
    assert torrents[0].seeding_time_limit is None


def test_all_torrents_include_completed_and_downloading_states():
    """Transmission 默认列表应同时包含已完成和下载中的任务状态。"""
    server = MagicMock()
    server.get_torrents.return_value = (
        [
            SimpleNamespace(
                name="Completed",
                download_dir="/mnt/raid5/home_lt999lt/video/downloads/movie",
                hashString="hash-completed",
                total_size=1024,
                labels=[],
                progress=100,
                status="seed_pending",
            ),
            SimpleNamespace(
                name="Downloading",
                download_dir="/mnt/raid5/home_lt999lt/video/downloads/movie",
                hashString="hash-downloading",
                total_size=2048,
                labels=[],
                progress=50,
                status="downloading",
                rate_download=1024,
                rate_upload=0,
                left_until_done=1024,
            ),
        ],
        False,
    )
    module = _build_transmission_module(server)

    torrents = module.list_torrents()

    assert ["completed", "downloading"] == [torrent.state for torrent in torrents]
    assert ["hash-completed", "hash-downloading"] == [
        torrent.hash for torrent in torrents
    ]
    server.get_torrents.assert_called_once_with(tags="moviepilot-tag")


def test_include_all_tags_removes_builtin_tag_filter():
    """查询全部标签任务时不应附加 MoviePilot 内置标签过滤。"""
    server = MagicMock()
    server.get_torrents.return_value = (
        [
            SimpleNamespace(
                name="External",
                download_dir="/mnt/raid5/home_lt999lt/video/downloads/movie",
                hashString="hash-external",
                total_size=1024,
                labels=["external"],
                progress=100,
                status="seeding",
            )
        ],
        False,
    )
    module = _build_transmission_module(server)

    torrents = module.list_torrents(include_all_tags=True)

    assert ["hash-external"] == [torrent.hash for torrent in torrents]
    server.get_torrents.assert_called_once_with(tags=None)
