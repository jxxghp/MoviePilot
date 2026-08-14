import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_transmission_client_module():
    """
    使用轻量桩加载 Transmission 客户端封装，避免测试依赖完整应用启动。
    """
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    log_module = types.ModuleType("app.runtime.log")
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    url_module = types.ModuleType("app.foundation.url")
    transmission_rpc_module = types.ModuleType("transmission_rpc")
    transmission_rpc_session_module = types.ModuleType("transmission_rpc.session")

    class _Logger:
        """
        测试日志桩，仅提供被客户端封装调用的方法。
        """
        def info(self, *_args, **_kwargs):
            """
            忽略信息日志。
            """
            pass

        def warning(self, *_args, **_kwargs):
            """
            忽略警告日志。
            """
            pass

        def error(self, *_args, **_kwargs):
            """
            忽略错误日志。
            """
            pass

    class _UrlUtils:
        """
        测试 URL 工具桩，满足按 URL 配置下载器时的解析接口。
        """
        @staticmethod
        def parse_url_params(url):
            """
            返回固定的 Transmission 连接参数。
            """
            return "http", url, 9091, ""

    log_module.logger = _Logger()
    url_module.UrlUtils = _UrlUtils
    transmission_rpc_module.Client = object
    transmission_rpc_module.Torrent = object
    transmission_rpc_module.File = object
    transmission_rpc_session_module.SessionStats = object
    transmission_rpc_session_module.Session = object

    app_module.log = log_module
    app_module.utils = utils_module
    utils_module.url = url_module

    stub_modules = {
        "app": app_module,
        "app.runtime.log": log_module,
        "app.utils": utils_module,
        "app.foundation.url": url_module,
        "transmission_rpc": transmission_rpc_module,
        "transmission_rpc.session": transmission_rpc_session_module,
    }

    transmission_path = repo_root / "app" / "modules" / "transmission" / "transmission.py"
    transmission_spec = importlib.util.spec_from_file_location(
        "app.modules.transmission.transmission",
        transmission_path,
    )
    transmission_module = importlib.util.module_from_spec(transmission_spec)
    assert transmission_spec and transmission_spec.loader

    with patch.dict(sys.modules, stub_modules):
        transmission_spec.loader.exec_module(transmission_module)

    return transmission_module


transmission_module = _load_transmission_client_module()
Transmission = transmission_module.Transmission


def test_login_enables_incomplete_file_suffix_by_default():
    """
    登录成功后默认开启未完成文件后缀，避免下载中的媒体文件被提前整理。
    """
    fake_client = MagicMock()
    fake_client.get_session.return_value = {"rename-partial-files": False}

    with patch.object(transmission_module.transmission_rpc, "Client", return_value=fake_client):
        downloader = Transmission(host="127.0.0.1", port=9091)

    assert downloader.trc is fake_client
    fake_client.set_session.assert_called_once_with(rename_partial_files=True)


def test_login_disables_incomplete_file_suffix_when_configured():
    """
    用户关闭配置后应同步关闭 Transmission 未完成文件后缀偏好。
    """
    fake_client = MagicMock()
    fake_client.get_session.return_value = types.SimpleNamespace(rename_partial_files=True)

    with patch.object(transmission_module.transmission_rpc, "Client", return_value=fake_client):
        downloader = Transmission(host="127.0.0.1", port=9091, rename_partial_files=False)

    assert downloader.trc is fake_client
    fake_client.set_session.assert_called_once_with(rename_partial_files=False)


def test_login_skips_incomplete_file_suffix_when_already_matches():
    """
    远端未完成文件后缀状态已匹配配置时不重复写入全局会话配置。
    """
    fake_client = MagicMock()
    fake_client.get_session.return_value = types.SimpleNamespace(rename_partial_files=True)

    with patch.object(transmission_module.transmission_rpc, "Client", return_value=fake_client):
        downloader = Transmission(host="127.0.0.1", port=9091)

    assert downloader.trc is fake_client
    fake_client.set_session.assert_not_called()


def test_get_files_uses_transmission_rpc_v7_get_files():
    """
    transmission-rpc v7 任务对象应使用 get_files 获取文件列表。
    """
    downloader = Transmission.__new__(Transmission)
    torrent_files = [object()]
    torrent = types.SimpleNamespace(get_files=MagicMock(return_value=torrent_files))
    fake_client = MagicMock()
    fake_client.get_torrent.return_value = torrent
    downloader.trc = fake_client

    assert downloader.get_files("1") == torrent_files
    fake_client.get_torrent.assert_called_once_with("1")
    torrent.get_files.assert_called_once_with()


def test_get_files_falls_back_to_legacy_files_method():
    """
    旧版 transmission-rpc 任务对象仍应通过 files 获取文件列表。
    """
    downloader = Transmission.__new__(Transmission)
    torrent_files = [object()]
    torrent = types.SimpleNamespace(files=MagicMock(return_value=torrent_files))
    fake_client = MagicMock()
    fake_client.get_torrent.return_value = torrent
    downloader.trc = fake_client

    assert downloader.get_files("1") == torrent_files
    fake_client.get_torrent.assert_called_once_with("1")
    torrent.files.assert_called_once_with()


def test_change_torrent_only_sends_explicit_fields():
    """
    修改单个任务时只能写入显式传入的策略字段。
    """
    downloader = Transmission.__new__(Transmission)
    fake_client = MagicMock()
    downloader.trc = fake_client

    assert downloader.change_torrent("hash", ratio_limit=2.5)

    fake_client.change_torrent.assert_called_once_with(
        ids="hash",
        seedRatioMode=1,
        seedRatioLimit=2.5,
    )


def test_change_torrent_disables_speed_limit_with_zero_value():
    """
    单任务限速传 0 时应显式关闭对应限速。
    """
    downloader = Transmission.__new__(Transmission)
    fake_client = MagicMock()
    downloader.trc = fake_client

    assert downloader.change_torrent("hash", download_limit=0, upload_limit=512)

    fake_client.change_torrent.assert_called_once_with(
        ids="hash",
        uploadLimited=True,
        uploadLimit=512,
        downloadLimited=False,
        downloadLimit=0,
    )


def test_set_torrent_location_prefers_move_torrent_data():
    """
    Transmission 修改保存目录应优先使用移动数据接口。
    """
    downloader = Transmission.__new__(Transmission)
    fake_client = MagicMock()
    downloader.trc = fake_client

    assert downloader.set_torrent_location("hash", "/downloads/new")

    fake_client.move_torrent_data.assert_called_once_with(
        ids="hash",
        location="/downloads/new",
    )
    fake_client.change_torrent.assert_not_called()


def test_set_torrent_location_falls_back_to_change_torrent():
    """
    旧版 transmission-rpc 没有移动数据接口时回退到 change_torrent。
    """
    downloader = Transmission.__new__(Transmission)
    fake_client = MagicMock()
    fake_client.move_torrent_data = None
    downloader.trc = fake_client

    assert downloader.set_torrent_location("hash", "/downloads/new")

    fake_client.change_torrent.assert_called_once_with(
        ids="hash",
        download_dir="/downloads/new",
    )
