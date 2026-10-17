"""
工作流空媒体、缺失评分及未知下载进度的边界行为。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.context import MediaInfo
from app.schemas.download import DownloadTask
from app.schemas.workflow import ActionContext
from app.workflow.actions import fetch_downloads as downloads_module
from app.workflow.actions.fetch_downloads import FetchDownloadsAction
from app.workflow.actions.filter_medias import FilterMediasAction


def test_missing_media_collection_finishes_empty():
    """
    Schema 允许的空媒体集合按零条输入完成过滤。
    """
    action = FilterMediasAction("filter")
    context = ActionContext(medias=None)
    assert action.execute(1, {}, context) is context
    assert context.medias == []
    assert action.success is True


@pytest.mark.parametrize("minimum", [0.0, 5.0])
def test_unknown_rating_cannot_satisfy_minimum(minimum):
    """
    指定最低评分时，缺失评分的条目不能被当作已达到要求。
    """
    action = FilterMediasAction("filter")
    context = ActionContext(medias=[MediaInfo(title="unknown", vote_average=None)])
    assert action.execute(1, {"vote": minimum}, context).medias == []
    assert action.success is True


@pytest.mark.parametrize("minimum,expected", [(None, ["unknown", "zero", "low", "edge", "high"]), (5.0, ["edge", "high"])])
def test_rating_filter_preserves_known_boundary_and_order(minimum, expected):
    """
    未启用评分过滤时保留所有条目，启用时保留相等边界及原始顺序。
    """
    context = ActionContext(medias=[MediaInfo(title=name, vote_average=score) for name, score in [("unknown", None), ("zero", 0.0), ("low", 4.9), ("edge", 5.0), ("high", 8.0)]])
    action = FilterMediasAction("filter")
    result = action.execute(1, {"vote": minimum}, context)
    assert [item.title for item in result.medias] == expected


@pytest.mark.parametrize("progress,completed", [(None, False), (0, False), (99.9, False), (100, True), (101, True)])
def test_unknown_download_progress_remains_incomplete(monkeypatch, progress, completed):
    """
    仅已知达到 100 的下载进度能完成任务，未知值保留为未完成。
    """
    chain = SimpleNamespace(list_torrents=lambda **_kwargs: [SimpleNamespace(path="/downloads/file.mkv", progress=progress)])
    monkeypatch.setattr(downloads_module, "ActionChain", lambda: chain)
    monkeypatch.setattr(downloads_module.runtime_stop_state, "is_workflow_stopped", lambda _workflow_id: False)
    action = FetchDownloadsAction("downloads")
    context = ActionContext(downloads=[DownloadTask(download_id="hash", downloader="qbittorrent")])
    assert action.execute(1, {}, context) is context
    assert context.downloads[0].completed is completed
    assert context.downloads[0].path == "/downloads/file.mkv"
    assert action.success is completed


@pytest.mark.parametrize("path,expected", [(Path("/downloads/file.mkv"), "/downloads/file.mkv"), (None, None), ("", "")])
def test_download_paths_remain_serializable_text(monkeypatch, path, expected):
    """
    下载链的 Path 转为工作流 Schema 的字符串，空值和已有字符串保持原义。
    """
    chain = SimpleNamespace(list_torrents=lambda **_kwargs: [SimpleNamespace(path=path, progress=100)])
    monkeypatch.setattr(downloads_module, "ActionChain", lambda: chain)
    monkeypatch.setattr(downloads_module.runtime_stop_state, "is_workflow_stopped", lambda _workflow_id: False)
    context = ActionContext(downloads=[DownloadTask(download_id="hash")])
    FetchDownloadsAction("downloads").execute(1, {}, context)
    assert context.downloads[0].path == expected
    assert context.model_dump(mode="json")["downloads"][0]["path"] == expected
