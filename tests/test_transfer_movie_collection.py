from types import SimpleNamespace

import pytest

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.schemas import DownloadHistory, FileItem, TransferTask
from app.schemas.types import MediaType


def _make_chain() -> TransferChain:
    """构造不启动后台线程的整理链测试实例。"""
    chain = object.__new__(TransferChain)
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._subtitle_exts + chain._audio_exts
    )
    chain.jobview = SimpleNamespace(
        finish_task=lambda task: None,
        try_remove_job=lambda task: None,
    )
    chain._TransferChain__get_trans_fileitems = lambda fileitem, predicate: [
        (fileitem, False)
    ]
    chain._TransferChain__put_to_jobview = lambda task: True
    chain._TransferChain__register_scrape_batch_task = lambda task: None
    chain._TransferChain__close_scrape_batch = lambda batch_id: None
    return chain


def _make_file_meta(year: str = "2013") -> SimpleNamespace:
    """构造电影合集文件的元数据。"""
    return SimpleNamespace(
        name="The Hunger Games Catching Fire",
        year=year,
        type=MediaType.UNKNOWN,
        begin_season=None,
        begin_episode=None,
        part=None,
    )


def _make_history() -> SimpleNamespace:
    """构造被合集首部电影占用的下载历史。"""
    return SimpleNamespace(
        id=1,
        download_hash="collection-hash",
        downloader="qbittorrent",
        type=MediaType.MOVIE.value,
        title="饥饿游戏",
        year="2012",
        tmdbid=70160,
        doubanid=None,
        episode_group=None,
        media_category=None,
        username=None,
        custom_words=None,
        note=None,
    )


def test_movie_year_conflict_only_applies_to_movies():
    """仅电影年份冲突应触发逐文件识别，电视剧季包仍复用下载历史。"""
    file_meta = _make_file_meta()
    movie_history = _make_history()
    tv_history = SimpleNamespace(type=MediaType.TV, year="2012")

    assert TransferChain._is_movie_year_conflict(file_meta, movie_history)
    assert not TransferChain._is_movie_year_conflict(file_meta, tv_history)
    movie_history.year = "2013"
    assert not TransferChain._is_movie_year_conflict(file_meta, movie_history)


def test_conflicting_download_history_recognizes_movie_by_file_meta(monkeypatch):
    """手动整理未指定媒体时，冲突的合集历史应回退到文件元数据识别。"""
    chain = object.__new__(TransferChain)
    fallback_media = MediaInfo(
        type=MediaType.MOVIE,
        title="饥饿游戏2：星火燎原",
        year="2013",
        tmdb_id=101299,
    )
    recognized_meta = []
    chain.recognize_media = lambda **kwargs: pytest.fail("不应按合集历史 ID 识别")
    chain.jobview = SimpleNamespace(
        migrate_task=lambda task: False,
        try_remove_job=lambda task: None,
    )
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(get_by_type_tmdbid=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        lambda: SimpleNamespace(
            recognize_by_meta=lambda meta, obtain_images: (
                recognized_meta.append(meta) or fallback_media
            )
        ),
    )
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/downloads/collection/The.Hunger.Games.Catching.Fire.2013.mkv",
            type="file",
            name="The.Hunger.Games.Catching.Fire.2013.mkv",
            extension="mkv",
            size=1024,
        ),
        meta=_make_file_meta(),
        download_history=DownloadHistory(**vars(_make_history())),
        preview=True,
    )

    state, message = chain._TransferChain__handle_transfer(task)

    assert not state
    assert "已在整理队列中" in message
    assert recognized_meta == [task.meta]
    assert task.mediainfo.tmdb_id == 101299


@pytest.mark.parametrize(
    ("manual", "expected_tmdb_id"),
    [
        (False, None),
        (True, 70160),
    ],
)
def test_movie_collection_conflict_only_drops_automatic_media(
        monkeypatch, manual: bool, expected_tmdb_id: int
):
    """自动整理应丢弃冲突的合集媒体，手动明确指定的媒体仍应保留。"""
    chain = _make_chain()
    source_file = FileItem(
        storage="local",
        path=(
            "/downloads/The.Hunger.Games.Complete.4-Film.Collection/"
            "The.Hunger.Games.Catching.Fire.2013.mkv"
        ),
        type="file",
        name="The.Hunger.Games.Catching.Fire.2013.mkv",
        extension="mkv",
        size=1024,
    )
    file_meta = _make_file_meta()
    history = _make_history()
    history_oper = SimpleNamespace(
        get_by_hash=lambda download_hash: history,
        get_file_by_fullpath=lambda fullpath: None,
        get_files_by_savepath=lambda savepath: [],
        get_by_path=lambda path: None,
    )
    captured_tasks = []

    def fake_handle_transfer(task, callback=None):
        """记录整理任务，避免执行真实文件操作。"""
        captured_tasks.append(task)
        return True, ""

    chain._TransferChain__handle_transfer = fake_handle_transfer
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: SimpleNamespace(get_by_src=lambda src, storage=None: None),
    )
    monkeypatch.setattr("app.chain.transfer.DownloadHistoryOper", lambda: history_oper)
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.StorageChain", lambda: SimpleNamespace())
    monkeypatch.setattr("app.chain.transfer.MetaInfoPath", lambda *args, **kwargs: file_meta)

    chain.do_transfer(
        fileitem=source_file,
        mediainfo=SimpleNamespace(
            tmdb_id=70160,
            type=MediaType.MOVIE,
            year="2012",
        ),
        download_hash=history.download_hash,
        background=False,
        manual=manual,
        preview=True,
    )

    assert len(captured_tasks) == 1
    task_media = captured_tasks[0].mediainfo
    assert getattr(task_media, "tmdb_id", None) == expected_tmdb_id
