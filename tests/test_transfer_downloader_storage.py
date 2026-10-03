"""下载器自动整理按存储身份读取源文件，不把远程路径当成本地路径。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.chain.transfer import queue as queue_module
from app.chain.transfer import workflow as workflow_module
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import DownloaderTorrent


@pytest.fixture
def monitored_source(monkeypatch):
    """只替换存储和下载器边界，执行真实的自动整理目录准入流程。"""
    directory = TransferDirectoryConf(
        storage="smb", download_path="/incoming", monitor_type="downloader",
        library_storage="smb", library_path="/media", transfer_type="copy",
    )
    source = FileItem(
        storage="smb", path="/incoming/Movie/movie.mkv", name="movie.mkv",
        type="file", size=1024, extension="mkv",
    )
    torrent = DownloaderTorrent(
        downloader="remote-qb", hash="hash-smb", path=Path("smb:/incoming/Movie/movie.mkv"),
    )
    storage = Mock()
    storage.get_file_item_strict.return_value = source
    monkeypatch.setattr(workflow_module, "StorageChain", Mock(return_value=storage))
    monkeypatch.setattr(queue_module.DirectoryHelper, "get_download_dirs", lambda _: [directory])
    chain = SimpleNamespace(
        jobview=Mock(), list_torrents=Mock(return_value=[torrent]),
        download_history_repository=Mock(), do_transfer=Mock(return_value=(True, "")),
        _build_transfer_fileitem=workflow_module.TransferWorkflowOwner._build_transfer_fileitem,
    )
    chain.jobview.get_all_torrent_hashes.return_value = set()
    chain.download_history_repository.get_by_hash.return_value = None
    return SimpleNamespace(chain=chain, storage=storage, source=source, torrent=torrent, directory=directory)


@pytest.mark.parametrize("storage_type", ["smb", "local"])
def test_downloader_monitor_reads_source_through_storage(monitored_source, storage_type):
    """仅有 SMB 监控目录也会整理；本地目录沿用同一存储查询合同。"""
    state = monitored_source
    state.directory.storage = storage_type
    state.source.storage = storage_type
    prefix = "smb:" if storage_type == "smb" else ""
    state.torrent.path = Path(f"{prefix}{state.source.path}")

    with patch.object(Path, "exists", side_effect=AssertionError("禁止入口查本地文件")), \
            patch.object(Path, "stat", side_effect=AssertionError("禁止入口读取本地属性")):
        result = queue_module.TransferQueueOwner.process(state.chain)

    assert result is True
    state.storage.get_file_item_strict.assert_called_once_with(
        storage=storage_type, path=Path("/incoming/Movie/movie.mkv"),
    )
    state.chain.do_transfer.assert_called_once_with(
        fileitem=state.source, mediainfo=None, mtype=None,
        downloader="remote-qb", download_hash="hash-smb",
    )


@pytest.mark.parametrize("task_path", [
    "/incoming/Movie/movie.mkv", "smb:/incoming2/movie.mkv", "smb:/incoming/../outside/movie.mkv",
])
def test_downloader_monitor_rejects_other_storage_or_sibling_prefix(monitored_source, task_path):
    """同名本地路径、相似目录前缀及越界路径不能命中远程下载目录。"""
    state = monitored_source
    state.torrent.path = Path(task_path)

    queue_module.TransferQueueOwner.process(state.chain)

    state.storage.get_file_item_strict.assert_not_called()
    state.chain.do_transfer.assert_not_called()


@pytest.mark.parametrize("failure", [None, StorageQueryError("SMB permission denied")])
def test_unavailable_source_is_not_enqueued_and_can_retry(monitored_source, failure):
    """文件缺失或存储查询失败不入队，恢复后下一次扫描仍能处理。"""
    state = monitored_source
    state.storage.get_file_item_strict.side_effect = [failure, state.source]
    # process 会释放下载器返回的列表，每次扫描需要返回新的列表。
    state.chain.list_torrents.side_effect = lambda **_: [state.torrent]

    queue_module.TransferQueueOwner.process(state.chain)
    state.chain.do_transfer.assert_not_called()
    queue_module.TransferQueueOwner.process(state.chain)

    state.chain.do_transfer.assert_called_once()
    assert state.storage.get_file_item_strict.call_count == 2


def test_unmapped_remote_directory_reports_actionable_warning(monitored_source, monkeypatch):
    """远程监控目录收到未映射的本地路径时，提示配置映射且不能误读本地文件。"""
    state = monitored_source
    state.torrent.path = Path("/incoming/Movie/movie.mkv")
    warning = Mock()
    monkeypatch.setattr(workflow_module.logger, "warning", warning)

    queue_module.TransferQueueOwner.process(state.chain)

    warning.assert_called_once()
    assert "路径映射" in warning.call_args.args[0]
    assert "remote-qb" in warning.call_args.args[0]
    state.storage.get_file_item_strict.assert_not_called()
    state.chain.do_transfer.assert_not_called()
