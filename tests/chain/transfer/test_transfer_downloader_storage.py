"""
下载器自动整理按存储身份读取源文件，不把远程路径当成本地路径。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

import pytest

from app.chain.transfer import queue as queue_module
from app.chain.transfer import workflow as workflow_module
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import DownloaderTorrent


@pytest.fixture
def monitored_source(monkeypatch):
    """
    只替换存储和下载器边界，执行真实的自动整理目录准入流程。
    """
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
    # 整理入口执行真实的 do_transfer / _execute_transfer 委托，只把最终的整理工作流替换为按真实签名校验参数的替身：
    # 轮询传了某一层不接受的参数时直接报 TypeError，而不是被宽松的 Mock 吞掉。
    owner = workflow_module.TransferWorkflowOwner
    run_workflow = create_autospec(owner._run_transfer_workflow, return_value=(True, ""))
    chain = SimpleNamespace(
        jobview=Mock(), list_torrents=Mock(return_value=[torrent]),
        download_history_repository=Mock(),
        _build_transfer_fileitem=owner._build_transfer_fileitem,
        # 整理轮询记录交给整理时的选中大小，打"已整理"标签前比对，见 test_transfer_mark_torrent_completed.py。
        _transfer_scan_sizes={},
    )
    chain._run_transfer_workflow = lambda *args, **kwargs: run_workflow(chain, *args, **kwargs)
    chain.do_transfer = Mock(side_effect=lambda *args, **kwargs: owner.do_transfer(chain, *args, **kwargs))
    chain._execute_transfer = Mock(side_effect=lambda *args, **kwargs: owner._execute_transfer(chain, *args, **kwargs))
    chain._transfer_downloader_task = (
        lambda *args: queue_module.TransferQueueOwner._transfer_downloader_task(chain, *args)
    )
    chain._remember_transfer_scan = (
        lambda item: queue_module.TransferQueueOwner._remember_transfer_scan(chain, item)
    )
    chain._transfer_scan_size_records = (
        lambda: queue_module.TransferQueueOwner._transfer_scan_size_records(chain)
    )
    chain.jobview.get_all_torrent_hashes.return_value = set()
    chain.download_history_repository.get_by_hash.return_value = None
    return SimpleNamespace(
        chain=chain, storage=storage, source=source, torrent=torrent, directory=directory, run_workflow=run_workflow,
    )


@pytest.mark.parametrize("storage_type", ["smb", "local"])
def test_downloader_monitor_reads_source_through_storage(monitored_source, storage_type):
    """
    仅有 SMB 监控目录也会整理；本地目录沿用同一存储查询合同。
    """
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
    # 没有未完成文件的任务照旧走公开的 do_transfer，替换或包装了它的插件看到的调用不变。
    state.chain.do_transfer.assert_called_once_with(
        fileitem=state.source, mediainfo=None, mtype=None, downloader="remote-qb", download_hash="hash-smb",
    )
    state.chain._execute_transfer.assert_not_called()
    state.run_workflow.assert_called_once()


def test_downloader_monitor_remembers_selected_size_for_tagging(monitored_source):
    """
    交给整理时记录任务的选中大小，整理结束打"已整理"标签前据此判断期间是否追加了新文件。
    """
    state = monitored_source
    state.torrent.selected_size = 2048

    with patch.object(Path, "exists", side_effect=AssertionError("禁止入口查本地文件")), \
            patch.object(Path, "stat", side_effect=AssertionError("禁止入口读取本地属性")):
        queue_module.TransferQueueOwner.process(state.chain)

    assert state.chain._transfer_scan_sizes == {("remote-qb", "hash-smb"): 2048}


def test_downloader_monitor_passes_incomplete_files_to_transfer(monitored_source):
    """
    只选中部分文件的任务，下载器报告尚未下载完成的文件随任务交给整理，整理时跳过。该参数是轮询内部参数，
    经 ``_execute_transfer`` 传到整理工作流，不进入公开的 ``do_transfer`` 签名。
    """
    state = monitored_source
    state.torrent.incomplete_files = ["movie.part2.mkv"]

    with patch.object(Path, "exists", side_effect=AssertionError("禁止入口查本地文件")), \
            patch.object(Path, "stat", side_effect=AssertionError("禁止入口读取本地属性")):
        queue_module.TransferQueueOwner.process(state.chain)

    state.chain.do_transfer.assert_not_called()
    state.chain._execute_transfer.assert_called_once()
    kwargs = state.run_workflow.call_args.kwargs
    assert kwargs["incomplete_files"] == ["movie.part2.mkv"]
    assert kwargs["fileitem"] is state.source
    assert kwargs["download_hash"] == "hash-smb"


def _file(path, *, file_type="file"):
    """
    构造整理候选文件或目录。
    """
    return FileItem(storage="local", path=path, name=Path(path).name, type=file_type, size=1024, extension="mkv")


_ROOT = _file("/media/incoming/Show.S01/", file_type="dir")


def test_candidates_skip_files_the_downloader_reports_incomplete():
    """
    E01 下完、E02 下到一半后被取消选择：残缺的 E02 仍在任务目录里，整理只能拿到 E01。下载器报告的是相对任务内容路径
    （整理根目录）的路径，拼成完整路径后逐级精确比较，文件名相似的其他文件不受影响。
    """
    e01, e02 = _file("/media/incoming/Show.S01/Show.S01E01.mkv"), _file("/media/incoming/Show.S01/Show.S01E02.mkv")
    lookalike = _file("/media/incoming/Show.S01/Extra.Show.S01E02.mkv")
    candidates = [(e01, False), (e02, False), (lookalike, False)]

    kept = workflow_module._drop_reported_incomplete(candidates, ["Show.S01E02.mkv"], _ROOT)

    assert kept == [(e01, False), (lookalike, False)]
    assert workflow_module._drop_reported_incomplete(candidates, None, _ROOT) is candidates
    assert workflow_module._drop_reported_incomplete(candidates, [], _ROOT) is candidates
    # Windows 风格分隔符同样能认出。
    windows_root = _file("C:\\media\\incoming\\Show.S01", file_type="dir")
    windows_e02 = _file("C:\\media\\incoming\\Show.S01\\Extras\\E02.mkv")
    assert workflow_module._drop_reported_incomplete([(windows_e02, False)], ["Extras\\E02.mkv"], windows_root) == []


def test_completed_file_with_same_name_in_other_directory_is_kept():
    """
    根目录的 E01.mkv 没下完、Extras/E01.mkv 已下完：只跳过根目录那个，同名但位于其他目录的完整文件照常整理。
    反过来报告 Extras/E01.mkv 未完成时，根目录的 E01.mkv 也不受影响。
    """
    root_e01 = _file("/media/incoming/Show.S01/E01.mkv")
    extras_e01 = _file("/media/incoming/Show.S01/Extras/E01.mkv")
    candidates = [(root_e01, False), (extras_e01, False)]

    assert workflow_module._drop_reported_incomplete(candidates, ["E01.mkv"], _ROOT) == [(extras_e01, False)]
    assert workflow_module._drop_reported_incomplete(candidates, ["Extras/E01.mkv"], _ROOT) == [(root_e01, False)]


@pytest.mark.parametrize("disc_path,incomplete,kept", [
    # 原盘位于任务目录下：盘内有文件没下完时整盘跳过，另一张完整的盘照常整理。
    ("/media/incoming/Show.S01/Disc1", ["Disc1/BDMV/STREAM/00002.m2ts"], False),
    ("/media/incoming/Show.S01/Disc1", ["Disc2/BDMV/STREAM/00002.m2ts"], True),
    # 任务目录本身就是原盘根目录。
    ("/media/incoming/Show.S01", ["BDMV/STREAM/00002.m2ts"], False),
])
def test_bluray_disc_with_incomplete_file_is_skipped_as_a_whole(disc_path, incomplete, kept):
    """
    蓝光原盘以整个原盘目录作为整理候选，盘内只要有一个文件没下完，整盘都不整理，不能把残缺原盘当成完整影片入库。
    """
    disc = _file(disc_path, file_type="dir")

    result = workflow_module._drop_reported_incomplete([(disc, True)], incomplete, _ROOT)

    assert result == ([(disc, True)] if kept else [])


@pytest.mark.parametrize("task_path", [
    "/incoming/Movie/movie.mkv", "smb:/incoming2/movie.mkv", "smb:/incoming/../outside/movie.mkv",
])
def test_downloader_monitor_rejects_other_storage_or_sibling_prefix(monitored_source, task_path):
    """
    同名本地路径、相似目录前缀及越界路径不能命中远程下载目录。
    """
    state = monitored_source
    state.torrent.path = Path(task_path)

    queue_module.TransferQueueOwner.process(state.chain)

    state.storage.get_file_item_strict.assert_not_called()
    state.chain.do_transfer.assert_not_called()


@pytest.mark.parametrize("failure", [None, StorageQueryError("SMB permission denied")])
def test_unavailable_source_is_not_enqueued_and_can_retry(monitored_source, failure):
    """
    文件缺失或存储查询失败不入队，恢复后下一次扫描仍能处理。
    """
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
    """
    远程监控目录收到未映射的本地路径时，提示配置映射且不能误读本地文件。
    """
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
