import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from unittest.mock import call, MagicMock, patch


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
    schemas_module.DownloaderTorrent = _DownloaderTorrent
    schema_types_module.TorrentStatus = TorrentStatus
    schema_types_module.TorrentQueryStatus = TorrentQueryStatus
    schema_types_module.DownloadTaskState = DownloadTaskState
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


def test_login_uses_api_key_header_without_auth_login():
    """API Key 登录时应使用 Bearer Header 并跳过用户名密码登录。"""
    fake_client = MagicMock()
    fake_client.app_version.return_value = "v5.2.0"

    with patch.object(qbittorrent_module.qbittorrentapi, "Client", return_value=fake_client) as client_cls:
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, apikey="secret-token")

    assert downloader.qbc is fake_client
    fake_client.auth_log_in.assert_not_called()
    fake_client.app_version.assert_called_once_with()
    assert client_cls.call_args.kwargs["EXTRA_HEADERS"] == {"Authorization": "Bearer secret-token"}


def test_login_enables_incomplete_file_suffix_by_default():
    """
    登录成功后默认开启未完成文件后缀，避免下载中的媒体文件被提前整理。
    """
    fake_client = MagicMock()
    fake_client.app_preferences.return_value = {"incomplete_files_ext": False}

    with patch.object(qbittorrent_module.qbittorrentapi, "Client", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    assert downloader.qbc is fake_client
    fake_client.app_set_preferences.assert_called_once_with({"incomplete_files_ext": True})


def test_login_disables_incomplete_file_suffix_when_configured():
    """
    用户关闭配置后应同步关闭 qBittorrent 未完成文件后缀偏好。
    """
    fake_client = MagicMock()
    fake_client.app_preferences.return_value = {"incomplete_files_ext": True}

    with patch.object(qbittorrent_module.qbittorrentapi, "Client", return_value=fake_client):
        downloader = Qbittorrent(
            host="http://127.0.0.1",
            port=8080,
            username="admin",
            password="adminadmin",
            incomplete_files_ext=False,
        )

    assert downloader.qbc is fake_client
    fake_client.app_set_preferences.assert_called_once_with({"incomplete_files_ext": False})


def test_login_skips_incomplete_file_suffix_when_already_matches():
    """
    远端未完成文件后缀状态已匹配配置时不重复写入全局偏好。
    """
    fake_client = MagicMock()
    fake_client.app_preferences.return_value = {"incomplete_files_ext": True}

    with patch.object(qbittorrent_module.qbittorrentapi, "Client", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    assert downloader.qbc is fake_client
    fake_client.app_set_preferences.assert_not_called()


def test_completed_status_includes_qbittorrent_finished_upload_states():
    """
    qBittorrent 按完成状态查询时应包含非下载中、非暂停的上传侧状态。
    """
    server = MagicMock()
    server.get_torrents.return_value = (
        [
            {
                "name": "QB Done",
                "content_path": "/downloads/QB Done",
                "hash": "hash-qb",
                "total_size": 1024,
                "completed": 1024,
                "progress": 1,
                "state": "stalledUP",
                "tags": "moviepilot-tag",
                "dlspeed": 0,
                "upspeed": 128,
            },
            {
                "name": "QB Downloading",
                "content_path": "/downloads/QB Downloading",
                "hash": "hash-downloading",
                "total_size": 2048,
                "completed": 1024,
                "progress": 0.5,
                "state": "queuedDL",
                "tags": "moviepilot-tag",
                "dlspeed": 64,
                "upspeed": 0,
            },
        ],
        False,
    )
    module = QbittorrentModule.__new__(QbittorrentModule)
    module.get_instances = MagicMock(return_value={"qb": server})
    module.normalize_return_path = MagicMock(side_effect=lambda path, _name: str(path))

    torrents = module.list_torrents(status="completed")

    assert [torrent.hash for torrent in torrents] == ["hash-qb"]
    assert torrents[0].state == "completed"
    server.get_torrents.assert_called_once_with(tags="moviepilot-tag")


def test_get_completed_torrents_includes_finished_stopped_tasks():
    """
    已完成但不再做种的 qBittorrent 任务仍应进入待整理列表。
    """
    fake_client = MagicMock()
    fake_client.torrents_info.return_value = [
        {
            "hash": "hash-stalled-up",
            "progress": 1,
            "amount_left": 0,
            "state": "stalledUP",
        },
        {
            "hash": "hash-stopped-up",
            "progress": 1,
            "amount_left": 0,
            "state": "stoppedUP",
        },
        {
            "hash": "hash-moving",
            "progress": 1,
            "amount_left": 0,
            "state": "moving",
        },
        {
            "hash": "hash-metadata",
            "progress": 0,
            "amount_left": 0,
            "state": "metaDL",
        },
        {
            "hash": "hash-download-left",
            "progress": 0.9,
            "amount_left": 1024,
            "state": "stalledDL",
        },
    ]

    with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    torrents = downloader.get_completed_torrents()

    assert [torrent["hash"] for torrent in torrents] == ["hash-stalled-up", "hash-stopped-up"]
    fake_client.torrents_info.assert_called_once_with(torrent_hashes=None, status_filter="completed")


def test_list_torrents_include_all_tags_removes_builtin_tag_filter():
    """
    智能体扩大查询范围时，qBittorrent 查询应取消内置标签过滤。
    """
    server = MagicMock()
    server.get_torrents.return_value = (
        [
            {
                "name": "External Task",
                "content_path": "/downloads/External Task",
                "hash": "hash-external",
                "total_size": 1024,
                "completed": 1024,
                "progress": 1,
                "state": "stalledUP",
                "tags": "external",
                "dlspeed": 0,
                "upspeed": 0,
            }
        ],
        False,
    )
    module = QbittorrentModule.__new__(QbittorrentModule)
    module.get_instances = MagicMock(return_value={"qb": server})
    module.normalize_return_path = MagicMock(side_effect=lambda path, _name: str(path))

    torrents = module.list_torrents(include_all_tags=True)

    assert [torrent.hash for torrent in torrents] == ["hash-external"]
    server.get_torrents.assert_called_once_with(tags=None)


def test_add_torrent_accepts_structured_success_response():
    """新版 qBittorrent API 结构化成功响应应返回新增种子 ID。"""
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
    assert success
    assert added_torrent_ids == ["abc123"]


def test_add_torrent_accepts_pending_success_response_without_ids():
    """新版 qBittorrent API 待处理成功响应没有 ID 时仍应视为添加成功。"""
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
    assert success
    assert added_torrent_ids == []


def test_add_torrent_uses_cookie_api_for_qbittorrent_52():
    """qBittorrent 5.2 对应 Web API 应通过 Cookie API 同步站点 Cookie。"""
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
    assert success
    assert added_torrent_ids == []
    set_cookie_call = fake_client.app_set_cookies.call_args.kwargs["cookies"]
    assert {
        "domain": "tracker.example.com",
        "path": "/",
        "name": "uid",
        "value": "1",
    } in set_cookie_call
    assert {
        "domain": "tracker.example.com",
        "path": "/",
        "name": "passkey",
        "value": "abc",
    } in set_cookie_call
    assert fake_client.torrents_add.call_args.kwargs["cookie"] is None


def test_add_torrent_keeps_legacy_cookie_param_for_old_webapi():
    """旧版 qBittorrent Web API 不支持 Cookie API 时保留添加种子 Cookie 参数。"""
    fake_client = MagicMock()
    fake_client.app_web_api_version.return_value = "2.11.2"
    fake_client.torrents_add.return_value = "Ok."

    with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    success, added_torrent_ids = downloader.add_torrent(
        content="https://tracker.example.com/download?id=1",
        cookie="uid=1",
    )
    assert success
    assert added_torrent_ids == []
    fake_client.app_set_cookies.assert_not_called()
    assert fake_client.torrents_add.call_args.kwargs["cookie"] == "uid=1"


def _build_module(server):
    """构造仅包含下载所需方法的 QbittorrentModule 测试实例。"""
    module = QbittorrentModule.__new__(QbittorrentModule)
    module.get_instance = MagicMock(return_value=server)
    module.normalize_path = MagicMock(side_effect=lambda path, _downloader: path)
    module.get_default_config_name = MagicMock(return_value="default-qb")
    return module


def test_download_prefers_added_torrent_ids_before_tag_lookup():
    """添加任务响应包含种子 ID 时应优先使用响应值。"""
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = (True, ["abc123"])
    fake_server.get_content_layout.return_value = "Original"
    fake_server.is_force_resume.return_value = False

    module = _build_module(fake_server)
    result = module.download(
        content="magnet:?xt=urn:btih:123",
        download_dir=Path("/downloads"),
        cookie="",
        downloader="qb",
    )

    assert result == ("qb", "abc123", "Original", "添加下载成功")
    fake_server.delete_torrents_tag.assert_called_once_with("abc123", "tmp-tag-01")
    fake_server.get_torrent_id_by_tag.assert_not_called()
    assert fake_server.add_torrent.call_args.kwargs["tag"] == ["tmp-tag-01", "moviepilot-tag"]
    assert fake_server.mock_calls.index(
        call.delete_torrents_tag("abc123", "tmp-tag-01")
    ) < fake_server.mock_calls.index(call.get_content_layout())


def test_download_falls_back_to_tag_lookup_when_added_ids_missing():
    """添加任务响应缺少种子 ID 时应回退到临时标签查询。"""
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = (True, [])
    fake_server.get_content_layout.return_value = "Original"
    fake_server.get_torrent_id_by_tag.return_value = "def456"
    fake_server.is_force_resume.return_value = False

    module = _build_module(fake_server)
    result = module.download(
        content="magnet:?xt=urn:btih:456",
        download_dir=Path("/downloads"),
        cookie="",
        downloader="qb",
    )

    assert result == ("qb", "def456", "Original", "添加下载成功")
    fake_server.delete_torrents_tag.assert_not_called()
    fake_server.get_torrent_id_by_tag.assert_called_once_with(tags="tmp-tag-01")


def test_download_removes_temporary_tag_from_existing_torrent():
    """重复添加任务时应从已存在的种子中删除本次临时标签。"""
    fake_server = MagicMock()
    fake_server.add_torrent.return_value = (False, [])
    fake_server.get_content_layout.return_value = "Original"
    fake_server.get_torrents.return_value = ([{
        "name": "test",
        "total_size": len(b"torrent-content"),
        "hash": "existing123",
        "tags": None,
    }], None)

    module = _build_module(fake_server)
    result = module.download(
        content=b"torrent-content",
        download_dir=Path("/downloads"),
        cookie="",
        downloader="qb",
    )

    assert result == ("qb", "existing123", "Original", "下载任务已存在")
    fake_server.delete_torrents_tag.assert_called_once_with("existing123", "tmp-tag-01")
    assert fake_server.mock_calls.index(
        call.delete_torrents_tag("existing123", "tmp-tag-01")
    ) < fake_server.mock_calls.index(call.get_content_layout())


def test_delete_torrents_tag_uses_supported_qbittorrent_api_arguments():
    """删除标签时应分别调用任务移除接口和全局标签删除接口。"""
    fake_client = MagicMock()
    downloader = Qbittorrent.__new__(Qbittorrent)
    downloader.qbc = fake_client

    assert downloader.delete_torrents_tag("abc123", "tmp-tag-01")
    fake_client.torrents_remove_tags.assert_called_once_with(
        torrent_hashes="abc123",
        tags="tmp-tag-01",
    )
    fake_client.torrents_delete_tags.assert_called_once_with(tags="tmp-tag-01")


def test_get_files_retries_until_qbittorrent_files_available():
    """qBittorrent 添加任务后文件列表短暂未就绪时应重试。"""
    torrent_files = [{"id": 12, "name": "Show.S01E12.mkv"}]
    fake_client = MagicMock()
    fake_client.torrents_files.side_effect = [
        Exception("Torrent hash(es): abc123"),
        torrent_files,
    ]

    with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    with patch.object(qbittorrent_module.time, "sleep") as sleep:
        result = downloader.get_files("abc123", retry=2, interval=1)

    assert result == torrent_files
    assert fake_client.torrents_files.call_count == 2
    fake_client.torrents_files.assert_called_with(torrent_hash="abc123")
    sleep.assert_called_once_with(1)


def test_download_episode_selection_retries_file_list_after_add():
    """按集选择下载时应等待 qBittorrent 刚添加的任务文件列表。"""

    class _EpisodeMetaInfo:
        """测试用集数识别对象。"""

        def __init__(self, name):
            self.episode_list = [12] if "E12" in name else [1]

    fake_server = MagicMock()
    fake_server.add_torrent.return_value = (True, ["abc123"])
    fake_server.get_content_layout.return_value = "Original"
    fake_server.get_files.return_value = [
        {"id": 1, "name": "Show.S01E01.mkv"},
        {"id": 12, "name": "Show.S01E12.mkv"},
    ]
    fake_server.is_force_resume.return_value = False

    module = _build_module(fake_server)
    with patch.object(qbittorrent_package_module, "MetaInfo", _EpisodeMetaInfo):
        result = module.download(
            content=b"torrent-content",
            download_dir=Path("/downloads"),
            cookie="",
            episodes={12},
            downloader="qb",
        )

    assert result == ("qb", "abc123", "Original", "添加下载成功，已选择集数：[12]")
    fake_server.get_files.assert_called_once_with("abc123", retry=5, interval=1)
    fake_server.set_files.assert_called_once_with(torrent_hash="abc123", file_ids=[1], priority=0)
    fake_server.start_torrents.assert_called_once_with("abc123")


def test_set_speed_limit_allows_single_direction_limit():
    """
    设置全局限速时允许只传一个方向，未传方向按不限速处理。
    """
    fake_client = MagicMock()

    with patch.object(Qbittorrent, "_Qbittorrent__login_qbittorrent", return_value=fake_client):
        downloader = Qbittorrent(host="http://127.0.0.1", port=8080, username="admin", password="adminadmin")

    assert downloader.set_speed_limit(download_limit=1024)
    assert fake_client.transfer.download_limit == 1024 * 1024
    assert fake_client.transfer.upload_limit == 0
