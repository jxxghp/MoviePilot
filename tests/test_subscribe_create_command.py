"""订阅新增事务所有权与默认入口集成测试。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.write import (
    AsyncCreateSubscriptionBatchCommand,
    AsyncCreateSubscriptionCommand,
    CreateSubscriptionCommand,
    SubscriptionCreateRequest,
    add_subscribe,
    async_add_subscribe,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
)
from app.db.adapters.subscription import (
    SessionSubscriptionBatchWriter,
    SessionSubscriptionRepository,
    TransactionalSubscriptionRepository,
)
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper, SubscribeStageResult
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


def _media(media_id: str) -> MediaInfo:
    """构造默认事务写入路径所需的最小媒体信息。"""
    media = MediaInfo()
    media.type = MediaType.MOVIE
    media.title = "事务测试电影"
    media.year = "2026"
    media.media_source = MediaSource.TMDB
    media.media_id = media_id
    media.vote_average = 8.0
    media.overview = "事务切片"
    return media


def _writer() -> TransactionalSubscriptionRepository:
    """按组合根合同构造显式短事务订阅仓储。"""
    return TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def _batch_request(
    media_id: str,
    season: int,
) -> SubscriptionCreateRequest:
    """构造真实数据库批量写测试使用的一季订阅请求。"""
    return SubscriptionCreateRequest(
        identity=SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id=media_id,
            season=season,
        ),
        payload=SubscriptionPatch({
            "name": "批量事务剧集",
            "year": "2026",
            "type": MediaType.TV.value,
            "media_source": str(MediaSource.TMDB),
            "media_id": media_id,
            "season": season,
            "username": "Seerr",
            "total_episode": 12,
            "lack_episode": 12,
        }),
        notification={"title": f"第 {season} 季订阅"},
    )


def test_sync_command_orders_stage_commit_before_caller_effect() -> None:
    """同步新增只有在仓储暂存和提交成功后才把结果交给外部副作用。"""
    calls: list[str] = []
    repository = Mock()
    repository.stage_add.side_effect = lambda *_: (
        calls.append("stage")
        or SubscribeStageResult(10, "新增订阅成功", True)
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = CreateSubscriptionCommand(repository, unit_of_work)

    result = command.execute({"media_id": "10"}, {"name": "demo"})
    calls.append("effect")

    assert result == (10, "新增订阅成功")
    assert calls == ["stage", "commit", "effect"]
    unit_of_work.rollback.assert_not_called()


def test_sync_command_rolls_back_commit_failure_and_skips_effect() -> None:
    """提交失败必须回滚并传播原异常，调用方不能误执行提交后副作用。"""
    commit_error = RuntimeError("commit failed")
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        11,
        "新增订阅成功",
        True,
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = commit_error
    command = CreateSubscriptionCommand(repository, unit_of_work)
    effects: list[str] = []

    with pytest.raises(RuntimeError) as raised:
        command.execute({"media_id": "11"}, {"name": "demo"})
        effects.append("effect")

    assert raised.value is commit_error
    assert effects == []
    unit_of_work.rollback.assert_called_once_with()


def test_sync_command_does_not_commit_duplicate_request() -> None:
    """查重命中沿用旧 ID 和消息，不开启无意义写事务。"""
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        12,
        "订阅已存在",
        False,
    )
    unit_of_work = Mock()
    command = CreateSubscriptionCommand(repository, unit_of_work)

    assert command.execute({}, {}) == (12, "订阅已存在")
    unit_of_work.commit.assert_not_called()
    unit_of_work.rollback.assert_not_called()


def test_sync_event_failure_does_not_roll_back_committed_subscription() -> None:
    """事件属于提交后副作用，失败只向上传播且不能伪装成数据库回滚。"""
    calls: list[str] = []
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        13,
        "新增订阅成功",
        True,
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    event_error = RuntimeError("event failed")

    def send_event(_subscribe_id: int) -> None:
        """模拟 Chain 在提交后发送订阅事件失败。"""
        calls.append("event")
        raise event_error

    command = CreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        command.execute({}, {}, after_commit=send_event)

    assert raised.value is event_error
    assert calls == ["commit", "event"]
    unit_of_work.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_async_command_rolls_back_staging_failure() -> None:
    """异步 flush 或唯一约束失败同样由命令回滚，不留部分写入。"""
    staging_error = RuntimeError("flush failed")
    repository = Mock()
    repository.async_stage_add = AsyncMock(side_effect=staging_error)
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    command = AsyncCreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        await command.execute({}, {})

    assert raised.value is staging_error
    unit_of_work.commit.assert_not_awaited()
    unit_of_work.rollback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_async_report_failure_happens_after_event_without_rollback() -> None:
    """异步上报失败保留事件先行顺序，也不回滚已经提交的订阅。"""
    calls: list[str] = []
    repository = Mock()
    repository.async_stage_add = AsyncMock(
        return_value=SubscribeStageResult(14, "新增订阅成功", True)
    )
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    unit_of_work.rollback = AsyncMock()
    report_error = RuntimeError("report failed")

    async def send_event_and_report(_subscribe_id: int) -> None:
        """模拟 Chain 先发事件再执行统计上报。"""
        calls.append("event")
        calls.append("report")
        raise report_error

    command = AsyncCreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        await command.execute({}, {}, after_commit=send_event_and_report)

    assert raised.value is report_error
    assert calls == ["commit", "event", "report"]
    unit_of_work.rollback.assert_not_awaited()


def test_async_batch_command_commits_once_and_stages_each_added_intent(db) -> None:
    """真实数据库中的多季订阅与各自事件、通知、统计 intent 只提交一次。"""
    db.watermark(Subscribe, OutboxMessage)
    async def execute() -> tuple[tuple[int, ...], list[int]]:
        """在真实 AsyncSession 中执行批量 writer 并记录提交后回调。"""
        async with async_session_scope() as session:
            actual_uow = SqlAlchemyAsyncUnitOfWork(session)
            unit_of_work = Mock()
            unit_of_work.commit = AsyncMock(wraps=actual_uow.commit)
            unit_of_work.rollback = AsyncMock(wraps=actual_uow.rollback)
            effects: list[int] = []

            async def after_commit(subscribe_id: int) -> bool:
                """证明冻结在每季请求内的副作用只在唯一提交后执行。"""
                assert unit_of_work.commit.await_count == 1
                effects.append(subscribe_id)
                return True

            requests = [
                replace(
                    _batch_request("servarr-batch-success", season),
                    after_commit=after_commit,
                )
                for season in (1, 2)
            ]
            writer = SessionSubscriptionBatchWriter(
                repository=SessionSubscriptionRepository(session),
                unit_of_work=unit_of_work,
                outbox=SqlAlchemyAsyncOutboxStager(session),
                dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(
                    async_session_scope
                ),
            )
            results = await writer.async_add(requests)
            unit_of_work.commit.assert_awaited_once_with()
            unit_of_work.rollback.assert_not_awaited()
            return tuple(result.subscribe_id for result in results), effects

    subscribe_ids, effects = asyncio.run(execute())

    assert effects == list(subscribe_ids)
    rows = db.session.execute(
        select(Subscribe)
        .where(Subscribe.media_id == "servarr-batch-success")
        .order_by(Subscribe.season)
    ).scalars().all()
    assert [(row.id, row.season) for row in rows] == [
        (subscribe_ids[0], 1),
        (subscribe_ids[1], 2),
    ]
    intents = db.session.execute(
        select(OutboxMessage)
        .where(OutboxMessage.event_key.contains("servarr-batch-success"))
        .order_by(OutboxMessage.id)
    ).scalars().all()
    assert [intent.topic for intent in intents] == [
        "subscribe.added",
        "subscribe.added.notification",
        "subscribe.added.report",
        "subscribe.added",
        "subscribe.added.notification",
        "subscribe.added.report",
    ]
    assert all(intent.status == "completed" for intent in intents)


def test_async_batch_command_rolls_back_all_rows_when_later_season_fails(db) -> None:
    """真实数据库中后一季暂存失败时回滚此前季和已经暂存的 outbox intents。"""
    db.watermark(Subscribe, OutboxMessage)
    requests = [
        _batch_request("servarr-batch-rollback", 1),
        _batch_request("servarr-batch-rollback", 2),
    ]
    failure = RuntimeError("second season failed")

    async def execute() -> None:
        """让第二次暂存失败，并使用真实 UoW 验证完整回滚。"""
        async with async_session_scope() as session:
            repository = SessionSubscriptionRepository(session)
            stage_calls = 0

            async def fail_second(*args, **kwargs):
                """第一季真实 flush，第二季在同一事务内抛出失败。"""
                nonlocal stage_calls
                stage_calls += 1
                if stage_calls == 2:
                    raise failure
                return await repository.async_stage_add(*args, **kwargs)

            failing_repository = Mock()
            failing_repository.async_stage_add = AsyncMock(side_effect=fail_second)
            actual_uow = SqlAlchemyAsyncUnitOfWork(session)
            unit_of_work = Mock()
            unit_of_work.commit = AsyncMock(wraps=actual_uow.commit)
            unit_of_work.rollback = AsyncMock(wraps=actual_uow.rollback)
            command = AsyncCreateSubscriptionBatchCommand(
                repository=failing_repository,
                unit_of_work=unit_of_work,
                outbox=SqlAlchemyAsyncOutboxStager(session),
            )

            with pytest.raises(RuntimeError) as raised:
                await command.execute(requests)

            assert raised.value is failure
            unit_of_work.commit.assert_not_awaited()
            unit_of_work.rollback.assert_awaited_once_with()

    asyncio.run(execute())

    rows = db.session.execute(
        select(Subscribe).where(
            Subscribe.media_id == "servarr-batch-rollback"
        )
    ).scalars().all()
    intents = db.session.execute(
        select(OutboxMessage).where(
            OutboxMessage.event_key.contains("servarr-batch-rollback")
        )
    ).scalars().all()
    assert rows == []
    assert intents == []


def test_default_sync_writer_persists_once_and_reuses_duplicate(db) -> None:
    """Chain 默认入口使用独立事务写入，重复媒体身份返回同一订阅。"""
    db.watermark(Subscribe)
    media = _media("arch-221-sync")
    after_commit = Mock()

    writer = _writer()
    first = add_subscribe(
        mediainfo=media,
        subscribe_oper=writer,
        after_commit=after_commit,
    )
    second = add_subscribe(
        mediainfo=media,
        subscribe_oper=writer,
        after_commit=after_commit,
    )

    assert first[0] > 0
    assert first[1] == "新增订阅成功"
    assert second == (first[0], "订阅已存在")
    db.session.expire_all()
    rows = Subscribe.list_by_media_identity(
        db.session,
        media_source=MediaSource.TMDB,
        media_id="arch-221-sync",
    )
    assert [row.id for row in rows] == [first[0]]
    assert after_commit.call_args_list == [((first[0],), {})]


def test_default_sync_writer_keeps_failed_report_pending_without_raising(db) -> None:
    """新增统计未确认时接口仍成功，事件 intent 收口而统计 intent 等待重试。"""
    db.watermark(Subscribe, OutboxMessage)
    media = _media("arch-221-report-pending")

    subscribe_id, message = add_subscribe(
        mediainfo=media,
        subscribe_oper=_writer(),
        after_commit=lambda _subscribe_id: False,
    )

    assert subscribe_id > 0
    assert message == "新增订阅成功"
    intents = db.session.execute(
        select(OutboxMessage)
        .where(OutboxMessage.event_key.contains(media.media_id))
        .order_by(OutboxMessage.id)
    ).scalars().all()
    assert [(intent.topic, intent.status) for intent in intents] == [
        ("subscribe.added", "completed"),
        ("subscribe.added.report", "pending"),
    ]


def test_default_async_writer_keeps_failed_report_pending_without_raising(db) -> None:
    """异步新增入口同样返回成功，并只留下统计 intent 等待重试。"""
    db.watermark(Subscribe, OutboxMessage)
    media = _media("arch-221-async-report-pending")

    async def report_failed(_subscribe_id: int) -> bool:
        """模拟异步统计接口未确认。"""
        return False

    subscribe_id, message = asyncio.run(
        async_add_subscribe(
            mediainfo=media,
            subscribe_oper=_writer(),
            after_commit=report_failed,
        )
    )

    assert subscribe_id > 0
    assert message == "新增订阅成功"
    intents = db.session.execute(
        select(OutboxMessage)
        .where(OutboxMessage.event_key.contains(media.media_id))
        .order_by(OutboxMessage.id)
    ).scalars().all()
    assert [(intent.topic, intent.status) for intent in intents] == [
        ("subscribe.added", "completed"),
        ("subscribe.added.report", "pending"),
    ]


def test_stage_add_reuses_explicit_session_without_commit(db, monkeypatch) -> None:
    """Oper 将调用方 Session 传给 Model 查询原语，暂存期间不自行提交。"""
    db.watermark(Subscribe)
    commit = Mock(wraps=db.session.commit)
    exists = Mock(wraps=Subscribe.exists)
    monkeypatch.setattr(db.session, "commit", commit)
    monkeypatch.setattr(Subscribe, "exists", exists)
    oper = SubscribeOper(db.session)
    identity = {
        "media_source": str(MediaSource.TMDB),
        "media_id": "arch-221-stage",
        "music_type": None,
        "season": None,
        "episode_group": None,
    }

    staged = oper.stage_add(
        identity,
        {
            "name": "Oper SQL",
            "type": MediaType.MOVIE.value,
            "state": "N",
            **identity,
        },
    )

    assert staged.created is True
    assert staged.subscribe_id > 0
    assert exists.call_args.args[0] is db.session
    commit.assert_not_called()
    db.session.rollback()


def test_default_async_writer_persists_committed_row(db) -> None:
    """Agent/API 使用的异步 Chain 入口在返回前已完成请求级提交。"""
    db.watermark(Subscribe)
    media = _media("arch-221-async")

    subscribe_id, message = asyncio.run(
        async_add_subscribe(mediainfo=media, subscribe_oper=_writer())
    )

    assert subscribe_id > 0
    assert message == "新增订阅成功"
    db.session.expire_all()
    persisted = Subscribe.get(db.session, subscribe_id)
    assert persisted is not None
    assert persisted.media_id == "arch-221-async"
