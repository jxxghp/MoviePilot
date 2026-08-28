"""DownloadChain 稳定导入身份与插件调用形态兼容测试。"""

import importlib
import inspect
import pickle
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.chain.base import ChainBase

# 包根通过 __getattr__ 惰性保留旧导入，Pylint 无法静态解析该符号。
from app.chain.download import DownloadChain  # pylint: disable=no-name-in-module
from app.chain.download.facade import DownloadChain as ConcreteDownloadChain
from app.runtime.events import Event
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import EventType


def test_download_static_and_dynamic_imports_share_stable_identity() -> None:
    """静态、动态和 concrete 导入必须指向同一个稳定类型。"""
    package = importlib.import_module("app.chain.download")
    dynamic = getattr(package, "DownloadChain")

    assert package.__all__ == ["DownloadChain"]
    assert DownloadChain is ConcreteDownloadChain
    assert dynamic is ConcreteDownloadChain
    assert DownloadChain.__module__ == "app.chain.download"
    assert pickle.loads(pickle.dumps(DownloadChain)) is DownloadChain


def test_download_chain_keeps_single_chain_base_and_inherited_list_torrents() -> None:
    """职责拆分不得复制 ChainBase 或遮蔽继承的种子查询入口。"""
    assert DownloadChain.__mro__.count(ChainBase) == 1
    assert DownloadChain.list_torrents is ChainBase.list_torrents
    assert inspect.signature(DownloadChain.list_torrents) == inspect.signature(
        ChainBase.list_torrents
    )


def test_download_single_accepts_existing_plugin_call_shapes() -> None:
    """单资源下载继续接受插件使用的上下文、选集与下载器参数。"""
    signature = inspect.signature(DownloadChain.download_single)

    signature.bind(
        object(),
        context=object(),
        torrent_file=object(),
        episodes={1, 2},
        source="plugin",
        downloader="qbittorrent",
        save_path="/media",
        userid="1",
        username="user",
    )
    assert signature.parameters["return_detail"].default is False
    assert signature.parameters["custom_words"].default is None


def test_batch_download_accepts_existing_plugin_call_shapes() -> None:
    """批量下载继续接受插件使用的缺集、来源和用户上下文参数。"""
    signature = inspect.signature(DownloadChain.batch_download)

    signature.bind(
        object(),
        contexts=[],
        no_exists={},
        save_path="/media",
        source="plugin",
        userid="1",
        username="user",
        downloader="qbittorrent",
        custom_words="S04E05 => S01E170",
    )


def test_get_no_exists_info_accepts_existing_plugin_call_shapes() -> None:
    """缺集查询继续接受插件使用的媒体信息和季度总集数参数。"""
    inspect.signature(DownloadChain.get_no_exists_info).bind(
        object(),
        meta=object(),
        mediainfo=object(),
        no_exists={},
        totals={1: 12},
    )


def test_inherited_list_torrents_accepts_existing_plugin_call_shapes() -> None:
    """继承的种子查询继续接受下载器、哈希、状态和全标签参数。"""
    inspect.signature(DownloadChain.list_torrents).bind(
        object(),
        downloader="qbittorrent",
        hashs=["hash"],
        status=object(),
        include_all_tags=True,
    )


def test_download_file_deleted_removes_task_without_deleting_files() -> None:
    """源文件删除后按 hash 清理任务并发布删除前快照。"""
    hash_string = "download-hash"
    torrent = DownloaderTorrent(
        downloader="qbittorrent",
        hash=hash_string,
        title="Demo.Release",
    )
    chain = DownloadChain.__new__(DownloadChain)
    chain.list_torrents = MagicMock(return_value=[torrent])
    chain.remove_torrents = MagicMock()
    chain.eventmanager = MagicMock()

    chain.download_file_deleted(
        Event(EventType.DownloadFileDeleted, {"hash": hash_string})
    )

    chain.list_torrents.assert_called_once_with(hashs=[hash_string])
    chain.remove_torrents.assert_called_once_with(
        hashs=[hash_string],
        delete_file=False,
    )
    chain.eventmanager.send_event.assert_called_once_with(
        EventType.DownloadDeleted,
        {"hash": hash_string, "torrents": [torrent.model_dump()]},
    )


@pytest.mark.parametrize(
    ("event", "expected_query"),
    [
        (None, False),
        (Event(EventType.DownloadFileDeleted, {"src": "/downloads/demo.mkv"}), False),
        (Event(EventType.DownloadFileDeleted, {"hash": "missing-hash"}), True),
    ],
)
def test_download_file_deleted_ignores_incomplete_or_missing_tasks(
    event: Optional[Event],
    expected_query: bool,
) -> None:
    """事件或任务信息不完整时不得删除任务或发布后续事件。"""
    chain = DownloadChain.__new__(DownloadChain)
    chain.list_torrents = MagicMock(return_value=[])
    chain.remove_torrents = MagicMock()
    chain.eventmanager = MagicMock()

    chain.download_file_deleted(event)

    if expected_query:
        chain.list_torrents.assert_called_once_with(hashs=["missing-hash"])
    else:
        chain.list_torrents.assert_not_called()
    chain.remove_torrents.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()
