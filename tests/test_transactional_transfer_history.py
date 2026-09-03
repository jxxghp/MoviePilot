"""整理历史类型化适配器的投影与事务测试。"""

import asyncio

import pytest

from app.application.history import (
    TransferHistoryMonthlyStatistics,
    TransferHistorySnapshot,
    TransferHistoryStatisticSnapshot,
    TransferHistoryWrite,
)
from app.db.adapters.history.transfer import (
    SessionTransferHistoryRepository,
    TransactionalTransferHistoryRepository,
)
from app.db.models.transferhistory import TransferHistory
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.types import MediaSource, MediaType


def _repository() -> TransactionalTransferHistoryRepository:
    """构造绑定测试数据库短 Session 的整理历史仓储。"""
    return TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def _history_write(
    *,
    src: str = "/downloads/Typed.Show.S01/Episode01.mkv",
    status: bool = True,
) -> TransferHistoryWrite:
    """构造覆盖整理、历史和 Agent 消费字段的类型化写入。"""
    return TransferHistoryWrite(
        src=src,
        src_storage="local",
        src_fileitem={
            "path": src,
            "size": 1024,
            "nested": {"parts": [1, 2]},
        },
        dest="/media/Typed Show (2026)/Season 01/Episode01.mkv",
        dest_storage="local",
        dest_fileitem={"path": "/media/Typed Show (2026)/Season 01/Episode01.mkv"},
        mode="copy",
        type=MediaType.TV.value,
        media_category_id="tv.episode",
        category="剧集",
        classification_rule_id="rule.tv.episode",
        classification_policy_revision=9,
        classification_source="automatic",
        title="Typed Show",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="7001",
        seasons="S01",
        episodes="E01",
        image="https://example.test/poster.jpg",
        downloader="qb",
        download_hash="typed-transfer-hash",
        status=status,
        errmsg=None if status else "transfer failed",
        files=["Episode01.mkv"],
        episode_group="group-1",
    )


def test_transactional_repository_projects_detached_snapshots(db) -> None:
    """同步查询应在 Session 内完成投影，并冻结所有嵌套 JSON。"""
    repository = _repository()
    created = repository.replace(_history_write())

    by_id = repository.get(created.id)
    by_src = repository.get_by_src(created.src or "", "local")
    successful = repository.get_success_by_src(created.src or "", "local")
    by_dest = repository.get_by_dest(created.dest or "", "local")
    by_identity = repository.get_by_media_identity(
        MediaSource.TMDB,
        "7001",
        MediaType.TV.value,
    )
    by_hash = repository.list_by_hash("typed-transfer-hash")

    assert isinstance(created, TransferHistorySnapshot)
    assert by_id == created
    assert by_src == created
    assert successful == created
    assert by_dest == created
    assert by_identity == created
    assert by_hash == [created]
    assert created.media_source == MediaSource.TMDB
    assert created.media_category_id == "tv.episode"
    assert created.category == "剧集"
    assert created.classification_rule_id == "rule.tv.episode"
    assert created.classification_policy_revision == 9
    assert created.classification_source == "automatic"
    assert not hasattr(created, "_sa_instance_state")

    assert isinstance(created.src_fileitem, dict)
    with pytest.raises(TypeError, match="不可修改"):
        created.src_fileitem["size"] = 2048
    nested = created.src_fileitem["nested"]
    assert isinstance(nested, dict)
    parts = nested["parts"]
    assert isinstance(parts, list)
    with pytest.raises(TypeError, match="不可修改"):
        parts.append(3)

    refreshed = repository.get(created.id)
    assert refreshed is not None
    assert refreshed.src_fileitem == {
        "path": created.src,
        "size": 1024,
        "nested": {"parts": [1, 2]},
    }


def test_transactional_repository_rolls_back_replace_on_commit_failure(
    db,
    monkeypatch,
) -> None:
    """替换提交失败时应恢复旧记录，不留下未提交的新投影。"""
    repository = _repository()
    original = repository.replace(_history_write())

    def fail_commit(_unit_of_work) -> None:
        """模拟数据库提交失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        repository.replace(
            _history_write(status=False),
        )

    current = repository.get_by_src(original.src or "", "local")
    assert current == original


def test_transactional_repository_sync_mutations_use_committed_uow(db) -> None:
    """Hash 更新、普通删除和清空应分别由独立事务完整提交。"""
    repository = _repository()
    first = repository.replace(_history_write(src="/downloads/first.mkv"))
    second = repository.replace(_history_write(src="/downloads/second.mkv"))

    repository.update_download_hash(first.id, "updated-hash")
    updated = repository.get(first.id)
    assert updated is not None
    assert updated.download_hash == "updated-hash"

    repository.delete(first.id)
    assert repository.get(first.id) is None
    assert repository.get(second.id) is not None

    repository.truncate()
    assert repository.get(second.id) is None


def test_transactional_repository_preserves_durable_history(db) -> None:
    """普通删除和清空不得破坏 durable 任务恢复证据。"""
    durable = db.add(
        TransferHistory(
            transfer_task_id="task-typed-history",
            transfer_settlement_revision=1,
            src="/downloads/durable.mkv",
            src_storage="local",
            status=False,
        )
    )
    repository = _repository()

    repository.delete(durable.id)
    repository.truncate()

    preserved = repository.get_by_transfer_task_id(task_id="task-typed-history")
    assert preserved is not None
    assert preserved.id == durable.id


def test_transactional_repository_async_queries_and_delete(db) -> None:
    """异步分页与计数返回快照，删除由独立异步事务提交。"""
    repository = _repository()
    created = repository.replace(_history_write(src="/downloads/async.mkv"))

    async def exercise() -> tuple[
        list[TransferHistorySnapshot],
        int,
        int,
        list[TransferHistoryStatisticSnapshot],
    ]:
        """在同一事件循环执行全部异步查询和删除。"""
        by_id = await repository.async_get(created.id)
        assert by_id == created
        by_title = await repository.async_list_by_title("Typed", count=10)
        by_page = await repository.async_list_by_page(count=10)
        total = await repository.async_count()
        matching = await repository.async_count_by_title("Typed")
        statistics = await repository.async_statistic()
        assert by_title == [created]
        await repository.async_delete(created.id)
        return by_page, total, matching, statistics

    records, total, matching, statistics = asyncio.run(exercise())

    assert created in records
    assert total >= 1
    assert matching >= 1
    assert all(isinstance(item, TransferHistoryStatisticSnapshot) for item in statistics)
    assert sum(item.count for item in statistics) >= 1
    assert all(not hasattr(record, "_sa_instance_state") for record in records)
    assert repository.get(created.id) is None


def test_transactional_repository_returns_typed_monthly_statistics(db) -> None:
    """月度聚合应返回命名 DTO，不向 Application 泄漏位置元组。"""
    repository = _repository()
    repository.replace(_history_write(src="/downloads/monthly.mkv"))

    statistics = repository.monthly_media_statistics()

    assert isinstance(statistics, TransferHistoryMonthlyStatistics)
    assert statistics.tv_shows >= 1
    assert statistics.episodes >= 1


def test_session_repository_obeys_request_uow(db) -> None:
    """请求级 adapter 只暂存删除，提交或回滚由请求 UoW 决定。"""
    repository = _repository()
    created = repository.replace(_history_write(src="/downloads/request.mkv"))

    with SessionFactory() as session:
        request_repository = SessionTransferHistoryRepository(session)
        assert request_repository.get(created.id) == created
        request_repository.stage_delete(created.id)
        session.rollback()
    assert repository.get(created.id) is not None

    with SessionFactory() as session:
        request_repository = SessionTransferHistoryRepository(session)
        request_repository.stage_truncate()
        session.commit()
    assert repository.get(created.id) is None
