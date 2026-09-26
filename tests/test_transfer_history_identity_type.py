"""下载历史媒体身份类型的整理回归测试。"""

from types import SimpleNamespace

from app.application.history import DownloadHistorySnapshot
from app.application.transfer.workflow import TransferTask
from app.chain.transfer import TransferChain  # pylint: disable=no-name-in-module
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.types import MediaSource, MediaType


def test_download_history_type_wins_over_monitor_type_hint(monkeypatch):
    """TMDB 影视 ID 冲突时，按下载历史保存的类型查询，不被目录提示改写。"""
    recognized_media = MediaInfo(type=MediaType.TV, title="兰香如故", year="2026")
    recognition_calls = []

    def recognize_media(**kwargs):
        """记录持久身份的实际识别类型。"""
        recognition_calls.append(kwargs)
        return recognized_media

    media_chain = SimpleNamespace(
        recognize_media=recognize_media,
        supplement_tmdb_info=lambda media, _meta: media,
    )
    monkeypatch.setattr(
        "app.chain.transfer.execution.MediaChain",
        lambda: media_chain,
    )

    chain = object.__new__(TransferChain)
    chain.transfer_history_repository = SimpleNamespace(
        get_by_media_identity=lambda **_kwargs: None,
    )
    chain.runtime_config = SimpleNamespace(scrape_follow_tmdb=True)
    chain.jobview = SimpleNamespace(
        migrate_task=lambda _task: False,
        try_remove_job=lambda _task: None,
    )
    chain.obtain_images = lambda **_kwargs: None

    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/downloads/兰香如故.S01E32.mkv",
            type="file",
            name="兰香如故.S01E32.mkv",
            extension="mkv",
            size=1024,
        ),
        meta=MetaBase("兰香如故 S01E32"),
        mtype=MediaType.MOVIE,
        download_history=DownloadHistorySnapshot(
            id=1,
            path="/downloads/兰香如故.S01E32.mkv",
            type=MediaType.TV.value,
            title="兰香如故",
            media_source=MediaSource.TMDB,
            media_id="282326",
        ),
        preview=True,
    )

    state, message = chain._TransferChain__handle_transfer(task)

    assert not state
    assert "已在整理队列中" in message
    assert recognition_calls[0]["mtype"] == MediaType.TV
    assert recognition_calls[0]["media_source"] == MediaSource.TMDB
    assert recognition_calls[0]["media_id"] == "282326"
