"""
验证同媒体不同种子的移动整理清理边界。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.chain.transfer.settlement as settlement_module
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.transfer import DownloaderFile, DownloaderTorrent, TransferInfo
from tests.support.transfer.jobs import (
    FakeMedia,
    bind_terminal_checkpoint,
    make_fileitem,
    make_task,
    make_transfer_chain,
)


@pytest.mark.parametrize("same_torrent", [False, True, None])
@pytest.mark.parametrize("sibling_state", ["failed", "running", "waiting"])
def test_successful_move_cleans_only_its_completed_torrent(
    monkeypatch, same_torrent, sibling_state,
):
    """
    不同种子的未完成任务不阻塞成功种子；同种子和无种子作业仍须全部成功。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.transfer_completed = MagicMock()
    chain.list_torrents = MagicMock(return_value=[])
    chain._can_delete_torrent = MagicMock(return_value=True)
    chain.remove_torrents = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.chain.transfer.settlement.get_configured_system_config", MagicMock(),
    )
    monkeypatch.setattr(
        "app.chain.transfer.settlement.add_transfer_success",
        lambda **_kwargs: SimpleNamespace(id=1),
    )
    current = make_task(17)
    sibling = make_task(16)
    current.download_hash = "completed-torrent" if same_torrent is not None else None
    sibling.download_hash = current.download_hash if same_torrent else "other-torrent"
    for task in (current, sibling):
        task.mediainfo = FakeMedia()
        task.downloader = "qb"
        assert chain._TransferChain__put_to_jobview(task)
    if sibling_state == "failed":
        chain.jobview.fail_task(sibling)
    elif sibling_state == "running":
        chain.jobview.running_task(sibling)
    result = TransferInfo(
        success=True,
        fileitem=current.fileitem,
        target_item=make_fileitem("/library/Test.Show.S01E17.mkv"),
        transfer_type="move",
        need_notify=False,
        need_scrape=False,
    )
    bind_terminal_checkpoint(current, result)

    state, error = chain._TransferChain__default_callback(current, result)

    assert state is True
    assert error == ""
    assert not chain.jobview.is_success(current)
    if same_torrent is not False:
        chain._can_delete_torrent.assert_not_called()
        chain.remove_torrents.assert_not_called()
    else:
        chain._can_delete_torrent.assert_called_once()
        chain.remove_torrents.assert_called_once_with("completed-torrent", downloader="qb")


def test_move_cleanup_checks_and_removes_under_task_lock(monkeypatch):
    """
    "检查可删除"与"删除任务"必须在同一把任务锁内完成：否则检查刚通过，下载链就在同一任务上追加了
    新文件，随后的删除会连同新文件一起删掉，订阅却已记成下载中。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.transfer_completed = MagicMock()
    chain.list_torrents = MagicMock(return_value=[])
    locks = settlement_module.downloader_task_locks
    stripe = locks._locks[locks._stripe("completed-torrent")]
    held = []
    chain._can_delete_torrent = MagicMock(side_effect=lambda *_args: held.append(stripe.locked()) or True)
    chain.remove_torrents = MagicMock(side_effect=lambda *_args, **_kwargs: held.append(stripe.locked()) or True)
    monkeypatch.setattr("app.chain.transfer.settlement.get_configured_system_config", MagicMock())
    monkeypatch.setattr(
        "app.chain.transfer.settlement.add_transfer_success",
        lambda **_kwargs: SimpleNamespace(id=1),
    )
    task = make_task(17)
    task.download_hash = "completed-torrent"
    task.mediainfo = FakeMedia()
    task.downloader = "qb"
    assert chain._TransferChain__put_to_jobview(task)
    result = TransferInfo(
        success=True,
        fileitem=task.fileitem,
        target_item=make_fileitem("/library/Test.Show.S01E17.mkv"),
        transfer_type="move",
        need_notify=False,
        need_scrape=False,
    )
    bind_terminal_checkpoint(task, result)

    chain._TransferChain__default_callback(task, result)

    assert held == [True, True]
    assert not stripe.locked()


@pytest.mark.parametrize("names,content,episode", [
    (["Show.S01E01.mkv", "Show.S01E02.mkv"], "Show.S01", "Show.S01/Show.S01E02.mkv"),
    (["Disc/Show.S01E01.mkv", "Disc/Show.S01E02.mkv"], "Show.S01", "Show.S01/Disc/Show.S01E02.mkv"),
    (["Show.S01/Show.S01E01.mkv", "Show.S01/Show.S01E02.mkv"], "Show.S01", "Show.S01/Show.S01E02.mkv"),
    (["Show.S01/Show.S01E01.mkv", "Show.S01/Show.S01E02.mkv"], "剧集/第一季", "剧集/第一季/Show.S01E02.mkv"),
    (["Show.S01E02.mkv"], "Show.S01E02.mkv", "Show.S01E02.mkv"),
    (["Show.S01E02.mkv"], "第二集.mkv", "第二集.mkv"),
    (["Show.S01/Show.S01E01.MKV", "Show.S01/Show.S01E02.MKV"], "Show.S01", "Show.S01/Show.S01E02.MKV"),
], ids=[
    "rtorrent-multi-file", "rtorrent-inner-folder", "save-path-relative", "mapping-renamed-content",
    "single-file", "mapping-renamed-single-file", "uppercase-extension",
])
@pytest.mark.parametrize("remaining", [True, False], ids=["episode-remaining", "all-moved"])
def test_move_cleanup_keeps_task_while_untransferred_media_remains(tmp_path, names, content, episode, remaining):
    """
    移动整理后删除任务前，按任务内容路径检查还有没有未整理的媒体文件，用真实文件验证。rTorrent 多文件任务的文件名相对
    任务目录本身、不带顶层目录，qBittorrent、Transmission 相对保存目录、带下载器侧的顶层目录名；路径映射把内容路径
    映射成另一个名字时，这级目录名与 MP 侧不同。任一种算错位置，都会把整理期间追加并下完、尚未整理的新集当成不存在，
    连同任务一起删掉。扩展名与整理收集候选时一样不区分大小写。E01 已移走，E02 还在时必须保留任务。
    """
    content_path = tmp_path / content
    (tmp_path / episode).parent.mkdir(parents=True, exist_ok=True)
    if remaining:
        (tmp_path / episode).write_bytes(b"episode")
    chain = make_transfer_chain()
    chain.list_torrents = MagicMock(return_value=[DownloaderTorrent(hash="hash-rt", path=content_path, progress=100)])
    chain.torrent_files = MagicMock(return_value=[DownloaderFile(name=name) for name in names])

    assert chain._can_delete_torrent("hash-rt", "rt", []) is not remaining


def test_move_cleanup_keeps_partially_selected_rtorrent_task(tmp_path):
    """
    rTorrent 只选了部分文件的任务，选中部分下完后进度仍按全部数据计算、不到 100：未选中的文件可能被写入了部分数据，
    移动整理后不删除任务。
    """
    chain = make_transfer_chain()
    chain.list_torrents = MagicMock(return_value=[
        DownloaderTorrent(hash="hash-rt", path=tmp_path / "Show.S01", progress=25.0, selected_completed=True),
    ])
    chain.torrent_files = MagicMock(return_value=[DownloaderFile(name="Show.S01E01.mkv")])

    assert chain._can_delete_torrent("hash-rt", "rt", []) is False
    chain.torrent_files.assert_not_called()


def _remote_cleanup_chain(present):
    """
    构造内容路径在 SMB 存储上的移动清理场景：存储查询只认 ``present`` 中的路径（路径到类型），其余确认不存在。
    """
    storage = MagicMock()
    storage.get_file_item_strict.side_effect = lambda storage, path: (
        FileItem(storage=storage, path=path.as_posix(), type=present[path.as_posix()])
        if path.as_posix() in present else None
    )
    chain = make_transfer_chain()
    chain._transfer_storage_chain = lambda: storage
    chain.list_torrents = MagicMock(return_value=[
        DownloaderTorrent(hash="hash-smb", path=Path("smb:/incoming/Show.S01"), progress=100),
    ])
    chain.torrent_files = MagicMock(return_value=[
        DownloaderFile(name="Show.S01/Show.S01E01.mkv"), DownloaderFile(name="Show.S01/Show.S01E02.mkv"),
    ])
    return chain, storage


@pytest.mark.parametrize("remaining", [True, False], ids=["episode-remaining", "all-moved"])
def test_move_cleanup_checks_remote_storage_through_storage_queries(remaining):
    """
    内容路径在远程存储（SMB）上时，本地文件系统永远查不到它，必须按整理入口相同的存储查询确认：E02 还在 SMB 上时
    保留任务，全部移走后照常删除。不能用本地 ``Path.exists`` 检查远程地址。
    """
    present = {"/incoming/Show.S01": "dir"}
    if remaining:
        present["/incoming/Show.S01/Show.S01E02.mkv"] = "file"
    chain, storage = _remote_cleanup_chain(present)

    with patch.object(Path, "exists", side_effect=AssertionError("远程地址不能查本地文件系统")), \
            patch.object(Path, "is_file", side_effect=AssertionError("远程地址不能查本地文件系统")):
        assert chain._can_delete_torrent("hash-smb", "qb", []) is not remaining

    assert {call.kwargs["storage"] for call in storage.get_file_item_strict.call_args_list} == {"smb"}


def test_move_cleanup_keeps_task_when_remote_storage_query_fails():
    """
    远程存储查询失败时无法核实文件还在不在，保留任务。
    """
    chain, storage = _remote_cleanup_chain({})
    storage.get_file_item_strict.side_effect = StorageQueryError("SMB permission denied")

    assert chain._can_delete_torrent("hash-smb", "qb", []) is False
