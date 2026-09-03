"""下载历史类型化适配器的投影与事务测试。"""

import asyncio

import pytest

from app.application.history import (
    DownloadFileSnapshot,
    DownloadFileWrite,
    DownloadHistorySnapshot,
    DownloadHistoryWrite,
)
from app.db.adapters.history.download import (
    SessionDownloadHistoryRepository,
    TransactionalDownloadHistoryRepository,
)
from app.db.models.downloadhistory import DownloadHistory
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.types import MediaSource, MediaType


def _repository() -> TransactionalDownloadHistoryRepository:
    """构造绑定测试数据库短 Session 的下载历史仓储。"""
    return TransactionalDownloadHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def _history_write(
    *,
    download_hash: str = "typed-history-hash",
) -> DownloadHistoryWrite:
    """构造覆盖 Chain 消费字段的类型化下载历史写入。"""
    return DownloadHistoryWrite(
        path="/downloads/Typed.Show.S01",
        type=MediaType.TV.value,
        title="Typed Show",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="7001",
        music_type=None,
        seasons="S01",
        episodes="E01-E02",
        image="https://example.test/backdrop.jpg",
        poster="https://example.test/poster.jpg",
        downloader="qb",
        download_hash=download_hash,
        torrent_name="Typed Show torrent",
        torrent_description="description",
        torrent_site="Example",
        userid=7,
        username="alice",
        channel="telegram",
        date="2026-08-28 10:00:00",
        note={
            "source": "subscribe",
            "nested": {"season": 1, "episodes": [1, 2]},
        },
        media_category_id="tv.episode",
        media_category="剧集",
        classification_rule_id="rule.tv.episode",
        classification_policy_revision=9,
        classification_source="automatic",
        episode_group="group-1",
        custom_words="S02 => S01",
    )


def _file_write(
    *,
    download_hash: str = "typed-history-hash",
) -> DownloadFileWrite:
    """构造与类型化历史关联的下载文件写入。"""
    return DownloadFileWrite(
        downloader="qb",
        download_hash=download_hash,
        fullpath="/downloads/Typed.Show.S01/Episode01.mkv",
        savepath="/downloads/Typed.Show.S01",
        filepath="Episode01.mkv",
        torrentname="Typed Show torrent",
    )


def test_transactional_repository_projects_detached_snapshots(db) -> None:
    """所有同步查询都应在 Session 内投影，且 JSON 不与后续查询共享。"""
    repository = _repository()
    history_id = repository.add(_history_write(), (_file_write(),))

    by_hash = repository.get_by_hash("typed-history-hash")
    by_path = repository.get_by_path("/downloads/Typed.Show.S01")
    by_hashes = repository.get_by_hashes(["typed-history-hash"])
    by_identity = repository.get_by_media_identity(
        MediaSource.TMDB,
        "7001",
    )
    by_fullpath = repository.get_file_by_fullpath("/downloads/Typed.Show.S01/Episode01.mkv")
    by_file_hash = repository.get_files_by_hash(
        "typed-history-hash",
        state=1,
    )
    by_savepath = repository.get_files_by_savepath("/downloads/Typed.Show.S01")

    assert isinstance(by_hash, DownloadHistorySnapshot)
    assert by_hash.id == history_id
    assert by_hash.userid == "7"
    assert by_hash.media_source == MediaSource.TMDB
    assert by_hash.media_category_id == "tv.episode"
    assert by_hash.media_category == "剧集"
    assert by_hash.classification_rule_id == "rule.tv.episode"
    assert by_hash.classification_policy_revision == 9
    assert by_hash.classification_source == "automatic"
    assert by_path == by_hash
    assert by_hashes == {"typed-history-hash": by_hash}
    assert by_identity == [by_hash]
    assert isinstance(by_fullpath, DownloadFileSnapshot)
    assert by_file_hash == [by_fullpath]
    assert by_savepath == [by_fullpath]
    assert not hasattr(by_hash, "_sa_instance_state")
    assert not hasattr(by_fullpath, "_sa_instance_state")

    assert isinstance(by_hash.note, dict)
    with pytest.raises(TypeError, match="不可修改"):
        by_hash.note["source"] = "mutated"
    nested = by_hash.note["nested"]
    assert isinstance(nested, dict)
    with pytest.raises(TypeError, match="不可修改"):
        nested["season"] = 2
    episodes = nested["episodes"]
    assert isinstance(episodes, list)
    with pytest.raises(TypeError, match="不可修改"):
        episodes.append(3)
    refreshed = repository.get_by_hash("typed-history-hash")
    assert refreshed is not None
    assert refreshed.note == {
        "source": "subscribe",
        "nested": {"season": 1, "episodes": [1, 2]},
    }


def test_transactional_repository_rolls_back_history_and_files_on_commit_failure(
    db,
    monkeypatch,
) -> None:
    """历史与文件提交失败时必须整体回滚，不得留下半写入记录。"""
    repository = _repository()
    download_hash = "typed-rollback-hash"

    def fail_commit(_unit_of_work) -> None:
        """模拟数据库提交失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        repository.add(
            _history_write(download_hash=download_hash),
            (_file_write(download_hash=download_hash),),
        )

    assert repository.get_by_hash(download_hash) is None
    assert repository.get_files_by_hash(download_hash) == []


def test_transactional_repository_async_query_and_delete(db) -> None:
    """异步分页返回脱离 Session 的快照，删除由独立事务提交。"""
    repository = _repository()
    baseline_count = asyncio.run(repository.async_count())
    history_id = repository.add(_history_write(download_hash="typed-async-hash"))

    async def exercise() -> list[DownloadHistorySnapshot]:
        """在同一事件循环中执行异步分页和删除。"""
        records = await repository.async_list_by_page(count=10)
        assert await repository.async_count() == baseline_count + 1
        await repository.async_delete(history_id)
        return records

    records = asyncio.run(exercise())

    assert any(record.id == history_id for record in records)
    assert all(not hasattr(record, "_sa_instance_state") for record in records)
    assert repository.get_by_hash("typed-async-hash") is None


def test_session_repository_obeys_caller_transaction(db) -> None:
    """请求级 adapter 只暂存变更，提交与回滚由调用方 UoW 决定。"""
    history = db.add(
        DownloadHistory(
            path="/downloads/request-history",
            type=MediaType.MOVIE.value,
            title="Request History",
            download_hash="request-history-hash",
        )
    )

    with SessionFactory() as session:
        repository = SessionDownloadHistoryRepository(session)
        repository.stage_delete_history(history.id)
        session.rollback()
    assert _repository().get_by_hash("request-history-hash") is not None

    with SessionFactory() as session:
        repository = SessionDownloadHistoryRepository(session)
        repository.stage_delete_history(history.id)
        session.commit()
    assert _repository().get_by_hash("request-history-hash") is None
