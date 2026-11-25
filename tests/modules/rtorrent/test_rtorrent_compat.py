"""
验证rTorrent 调用协议与下载任务状态兼容。
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_rtorrent_client_module():
    """
    使用轻量桩加载 rTorrent 客户端封装，避免测试依赖完整应用启动。
    """
    repo_root = Path(__file__).resolve().parents[3]

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    log_module = types.ModuleType("app.runtime.log")

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

    log_module.logger = _Logger()
    app_module.log = log_module

    stub_modules = {
        "app": app_module,
        "app.runtime.log": log_module,
    }

    rtorrent_path = repo_root / "app" / "modules" / "rtorrent" / "rtorrent.py"
    rtorrent_spec = importlib.util.spec_from_file_location(
        "app.modules.rtorrent.rtorrent",
        rtorrent_path,
    )
    rtorrent_module = importlib.util.module_from_spec(rtorrent_spec)
    assert rtorrent_spec and rtorrent_spec.loader

    with patch.dict(sys.modules, stub_modules):
        rtorrent_spec.loader.exec_module(rtorrent_module)

    return rtorrent_module


rtorrent_module = _load_rtorrent_client_module()
Rtorrent = rtorrent_module.Rtorrent


def test_change_torrent_sets_per_task_speed_limits():
    """
    rTorrent 单任务限速应创建限速组并绑定到任务。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.change_torrent(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        download_limit=1024,
        upload_limit=512,
    )

    fake_proxy.throttle.down.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        1024 * 1024,
    )
    fake_proxy.throttle.up.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        512 * 1024,
    )
    fake_proxy.d.throttle_name.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "mp_abcdef1234567890",
    )


def test_change_torrent_allows_zero_limit_to_disable_limit():
    """
    rTorrent 单任务限速传 0 时应写入 0，表示关闭对应限速。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.change_torrent(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        download_limit=0,
    )

    fake_proxy.throttle.down.max.set.assert_called_once_with(
        "mp_abcdef1234567890",
        0,
    )
    fake_proxy.throttle.up.max.set.assert_not_called()
    fake_proxy.d.throttle_name.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "mp_abcdef1234567890",
    )


def test_set_torrent_location_updates_directory():
    """
    rTorrent 保存目录修改应调用 d.directory.set。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.set_torrent_location(
        hash_string="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        location="/downloads/new",
    )

    fake_proxy.d.directory.set.assert_called_once_with(
        "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        "/downloads/new",
    )


def test_set_files_flushes_written_priorities_after_partial_failure():
    """
    逐个写入优先级中途失败时，已写入的部分仍要刷新生效并返回失败，由调用方重新读取实际状态。

    否则重试时读到这些文件已是"选中"不会再写入，优先级却始终没有生效。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    fake_proxy.f.priority.set.side_effect = [None, RuntimeError("连接中断")]
    downloader._proxy = fake_proxy

    assert downloader.set_files(torrent_hash="ABCDEF", file_ids=[1, 2], priority=1) is False

    fake_proxy.d.update_priorities.assert_called_once_with("ABCDEF")


def test_set_files_without_any_write_skips_priority_refresh():
    """
    第一个文件就写入失败时没有需要刷新的优先级，不额外发送刷新请求。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    fake_proxy.f.priority.set.side_effect = RuntimeError("连接中断")
    downloader._proxy = fake_proxy

    assert downloader.set_files(torrent_hash="ABCDEF", file_ids=[1], priority=1) is False

    fake_proxy.d.update_priorities.assert_not_called()


def test_update_priorities_reports_request_result():
    """
    单独刷新文件优先级时如实返回请求是否完成。
    """
    downloader = Rtorrent.__new__(Rtorrent)
    fake_proxy = MagicMock()
    downloader._proxy = fake_proxy

    assert downloader.update_priorities("ABCDEF") is True
    fake_proxy.d.update_priorities.side_effect = RuntimeError("连接中断")
    assert downloader.update_priorities("ABCDEF") is False
