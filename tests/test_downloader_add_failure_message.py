"""下载器添加任务失败时的提示文本。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule

_ANNOUNCE = b"https://tracker.invalid/announce?passkey=0123456789abcdef"
_PIECES = bytes(range(20))
_TORRENT = (
    b"d8:announce" + str(len(_ANNOUNCE)).encode() + b":" + _ANNOUNCE
    + b"4:infod6:lengthi1024e4:name8:test.mkv12:piece lengthi16384e"
    + b"6:pieces" + str(len(_PIECES)).encode() + b":" + _PIECES + b"ee"
)
_MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"


def _build_module(module_cls, server):
    """构造只包含下载所需依赖的下载器模块。"""
    module = module_cls.__new__(module_cls)
    module.get_instance = MagicMock(return_value=server)
    module.normalize_path = MagicMock(side_effect=lambda path, _downloader: str(path))
    module.get_default_config_name = MagicMock(return_value="default")
    return module


def _rejecting_server(module_cls):
    """下载器拒绝添加，且没有同名同大小的已有任务。"""
    server = MagicMock()
    if module_cls is QbittorrentModule:
        server.add_torrent.return_value = (False, [])
    elif module_cls is TransmissionModule:
        server.add_torrent.return_value = None
    else:
        server.add_torrent.return_value = False
    server.get_torrents.return_value = ([], False)
    return server


@pytest.mark.parametrize("module_cls", [QbittorrentModule, TransmissionModule, RtorrentModule])
@pytest.mark.parametrize(
    ("content", "expected"),
    [(_TORRENT, "添加种子任务失败：test.mkv"), (_MAGNET, f"添加种子任务失败：{_MAGNET}")],
    ids=["torrent", "magnet"],
)
def test_rejected_download_reports_torrent_name_or_magnet(module_cls, content, expected):
    """种子文件被拒时提示种子名称，磁力链接保留原文，不拼接种子二进制内容。"""
    module = _build_module(module_cls, _rejecting_server(module_cls))

    result = module.download(content=content, download_dir=Path("/downloads"), cookie="")

    assert result == (None, None, None, expected)


@pytest.mark.parametrize(
    ("module_cls", "expected"),
    [
        (QbittorrentModule, "下载任务添加成功，但获取Qbittorrent任务信息失败：test.mkv"),
        (RtorrentModule, "下载任务添加成功，但获取rTorrent任务信息失败：test.mkv"),
    ],
    ids=["qbittorrent", "rtorrent"],
)
def test_added_download_without_hash_reports_torrent_name(module_cls, expected):
    """添加成功但查不到任务时，提示同样使用种子名称。"""
    server = MagicMock()
    server.add_torrent.return_value = (True, []) if module_cls is QbittorrentModule else True
    server.get_torrent_id_by_tag.return_value = None
    module = _build_module(module_cls, server)

    result = module.download(content=_TORRENT, download_dir=Path("/downloads"), cookie="")

    assert result == (None, None, None, expected)
