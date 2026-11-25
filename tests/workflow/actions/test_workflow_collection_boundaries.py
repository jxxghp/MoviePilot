"""
验证工作流空集合、读取失败与不完整资源的副作用边界。
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.context import Context as WorkflowContext
from app.schemas.download import DownloadTask
from app.schemas.file import FileItem
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext
from app.workflow.actions import add_download, add_subscribe, scan_file, transfer_file
from app.workflow.actions.add_download import AddDownloadAction
from app.workflow.actions.add_subscribe import AddSubscribeAction
from app.workflow.actions.scan_file import ScanFileAction
from app.workflow.actions.transfer_file import TransferFileAction


def test_absent_subscription_input_does_not_create_chain(monkeypatch):
    """
    空媒体输入正常完成，不能触发订阅服务调用。
    """
    factory = Mock(side_effect=AssertionError("unexpected subscription access"))
    monkeypatch.setattr(add_subscribe, "SubscribeChain", factory)
    action = AddSubscribeAction("subscribe")
    context = ActionContext(medias=None)
    assert action.execute(1, {}, context) is context
    assert action.success is True
    assert action.done is True
    factory.assert_not_called()


def test_retained_subscription_results_can_be_read_without_new_media(monkeypatch):
    """
    循环动作的已有结果可在下一次空输入中读取，避免引用未初始化的链对象。
    """
    chain = Mock()
    chain.subscription_repository.get.return_value = None
    factory = Mock(return_value=chain)
    monkeypatch.setattr(add_subscribe, "SubscribeChain", factory)
    action = AddSubscribeAction("subscribe")
    action._added_subscribes = [7]
    context = ActionContext(medias=[])
    assert action.execute(1, {}, context) is context
    chain.subscription_repository.get.assert_called_once_with(7)
    chain.add.assert_not_called()


@pytest.fixture
def storage(monkeypatch):
    """
    替换目录扫描与文件整理使用的存储链，避免访问真实文件系统。
    """
    chain = Mock()
    chain.get_file_item.return_value = FileItem(storage="local", type="file", path="/source/movie.mkv", name="movie.mkv", extension="mkv")
    monkeypatch.setattr(scan_file, "StorageChain", lambda: chain)
    monkeypatch.setattr(transfer_file, "StorageChain", lambda: chain)
    monkeypatch.setattr(scan_file, "get_chain_runtime_config_snapshot", lambda: SimpleNamespace(media_extensions=[".mkv"], subtitle_extensions=[], audio_extensions=[]))
    return chain


@pytest.mark.parametrize("files,success", [(None, False), ([], True)])
def test_scan_distinguishes_failed_listing_from_empty_directory(storage, files, success):
    """
    读取失败必须失败，成功读取到空目录才允许报告零条结果。
    """
    storage.list_files.return_value = files
    action = ScanFileAction("scan")
    context = ActionContext(fileitems=None)
    assert action.execute(1, {"directory": "/source"}, context) is context
    assert action.success is success
    assert action.done is True
    assert context.fileitems is None


def test_scan_initializes_absent_output_only_when_files_are_found(storage):
    """
    首次扫描到文件时建立输出列表，并保留文件对象。
    """
    file = storage.get_file_item.return_value
    storage.list_files.return_value = [file]
    action = ScanFileAction("scan")
    context = ActionContext(fileitems=None)
    assert action.execute(1, {"directory": "/source"}, context) is context
    assert context.fileitems == [file]
    assert context.fileitems[0] is file
    assert action.success is True


@pytest.fixture
def transfer(monkeypatch, storage):
    """
    使用无历史记录且成功返回的整理链，单独验证动作的输入边界。
    """
    chain = Mock()
    chain.transfer_history_repository.get_by_src.return_value = None
    chain.do_transfer.return_value = (True, "")
    monkeypatch.setattr(transfer_file, "TransferChain", lambda: chain)
    action = TransferFileAction("transfer")
    action.check_cache = Mock(return_value=False)
    action.save_cache = Mock()
    return action, chain, storage


@pytest.mark.parametrize("source", ["downloads", "fileitems"])
def test_absent_transfer_collections_do_not_process_files(transfer, source):
    """
    空下载或文件集合不应崩溃，也不能执行整理。
    """
    action, chain, storage = transfer
    context = ActionContext(downloads=None, fileitems=None)
    assert action.execute(1, {"source": source}, context) is context
    assert action.success is True
    storage.get_file_item.assert_not_called()
    chain.do_transfer.assert_not_called()


@pytest.mark.parametrize("path", [None, ""])
def test_completed_download_without_path_is_not_transferred(transfer, path):
    """
    缺失下载路径不能被解释成当前目录，也不能写入成功缓存。
    """
    action, chain, storage = transfer
    context = ActionContext(downloads=[DownloadTask(download_id="hash", completed=True, path=path)])
    assert action.execute(1, {}, context) is context
    assert action.success is False
    storage.get_file_item.assert_not_called()
    chain.do_transfer.assert_not_called()
    action.save_cache.assert_not_called()


def test_download_transfer_initializes_absent_file_output(transfer):
    """
    下载整理成功后建立缺失的文件输出列表，并正常记录缓存。
    """
    action, chain, storage = transfer
    context = ActionContext(downloads=[DownloadTask(download_id="hash", completed=True, path="/source/movie.mkv")], fileitems=None)
    assert action.execute(1, {}, context) is context
    assert context.fileitems == [storage.get_file_item.return_value]
    assert action.success is True
    chain.do_transfer.assert_called_once()
    action.save_cache.assert_called_once_with(1, "hash")


def test_transfer_preserves_existing_output_list_and_callback(transfer):
    """
    保留已有列表身份、成功缓存和协作取消回调。
    """
    action, chain, storage = transfer
    context = ActionContext(fileitems=[storage.get_file_item.return_value])
    original = context.fileitems
    assert action.execute(1, {"source": "fileitems"}, context) is context
    assert context.fileitems is original
    assert len(context.fileitems) == 1
    assert chain.do_transfer.call_args.kwargs["continue_callback"]() is True
    action.save_cache.assert_called_once_with(1, "/source/movie.mkv")


@pytest.fixture
def download(monkeypatch):
    """
    替换下载与媒体识别链，检查不完整输入不能触发外部调用。
    """
    chain = Mock()
    chain.download_single.return_value = "hash"
    factory = Mock(return_value=chain)
    media = Mock()
    monkeypatch.setattr(add_download, "DownloadChain", factory)
    monkeypatch.setattr(add_download, "MediaChain", media)
    action = AddDownloadAction("download")
    action.check_cache = Mock(return_value=False)
    action.save_cache = Mock()
    return action, chain, media


def test_download_rejects_missing_torrent_before_lookup(download):
    """
    缺失种子信息的资源保留失败状态，不能查询或提交下载。
    """
    action, chain, media = download
    context = ActionContext(torrents=[WorkflowContext(torrent_info=None)])
    assert action.execute(1, {}, context) is context
    assert action.success is False
    chain.download_single.assert_not_called()
    media.assert_not_called()
    action.save_cache.assert_not_called()


def test_download_rejects_missing_title_when_metadata_is_absent(download):
    """
    缺少标题且没有既有元数据时直接失败，避免调用识别器处理无效标题。
    """
    action, chain, media = download
    context = ActionContext(torrents=[WorkflowContext(torrent_info={"title": None})])
    assert action.execute(1, {}, context) is context
    assert action.success is False
    chain.download_single.assert_not_called()
    media.assert_not_called()


@pytest.mark.parametrize("title", ["Movie", None])
def test_download_keeps_existing_metadata_and_initializes_output(download, title):
    """
    已有元数据可继续用于无标题资源，成功结果写入缺失的下载列表。
    """
    action, chain, media = download
    resource = Context(meta_info=MetaInfo("Movie"), media_info=MediaInfo(title="Movie", type=MediaType.MOVIE, tmdb_id=123), torrent_info=TorrentInfo(title="Movie"))
    resource.torrent_info.title = title
    context = ActionContext(downloads=None)
    context.torrents = [resource]
    assert action.execute(1, {}, context) is context
    assert action.success is True
    assert context.downloads[0].download_id == "hash"
    assert chain.download_single.call_args.kwargs["context"] is resource
    media.assert_not_called()
