import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_transmission_client_module():
    """
    使用轻量桩加载 Transmission 客户端封装，避免测试依赖完整应用启动。
    """
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    log_module = types.ModuleType("app.log")
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = []
    url_module = types.ModuleType("app.utils.url")
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
        "app.log": log_module,
        "app.utils": utils_module,
        "app.utils.url": url_module,
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


class TestTransmissionCompat(unittest.TestCase):
    def test_login_enables_incomplete_file_suffix(self):
        """
        登录成功后应开启未完成文件后缀，避免下载中的媒体文件被提前整理。
        """
        fake_client = MagicMock()
        fake_client.get_session.return_value = {"rename-partial-files": False}

        with patch.object(transmission_module.transmission_rpc, "Client", return_value=fake_client):
            downloader = Transmission(host="127.0.0.1", port=9091)

        self.assertIs(downloader.trc, fake_client)
        fake_client.set_session.assert_called_once_with(rename_partial_files=True)

    def test_login_skips_incomplete_file_suffix_when_already_enabled(self):
        """
        远端已开启未完成文件后缀时不重复写入全局会话配置。
        """
        fake_client = MagicMock()
        fake_client.get_session.return_value = types.SimpleNamespace(rename_partial_files=True)

        with patch.object(transmission_module.transmission_rpc, "Client", return_value=fake_client):
            downloader = Transmission(host="127.0.0.1", port=9091)

        self.assertIs(downloader.trc, fake_client)
        fake_client.set_session.assert_not_called()
