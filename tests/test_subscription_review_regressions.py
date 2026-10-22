"""
订阅身份、并发新增、过期取消搜索及切换目标的真实持久化回归。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.servarr import arr_add_series
from app.application.servarr import ServarrSubscriptionService
from app.application.subscription.contract import SubscriptionIdentityConflictError, SubscriptionSnapshot
from app.application.subscription.mutation import (
    SubscriptionActor,
    SyncSubscriptionMutationService,
    _normalized_subscription_patch,
)
from app.application.subscription.query import SubscriptionQueryService
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.db.models.searchsession import SearchSession
from app.db.models.subscribe import Subscribe
from app.db.models.subscriptionsearch import SubscriptionSearchBatch, SubscriptionSearchTask
from app.db.oper.subscribe import SubscribeOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.schemas.servarr import SonarrSeason, SonarrSeries
from app.schemas.types import MediaSource


@pytest.fixture
def database(tmp_path):
    """
    每次创建隔离 SQLite 及独立事务工厂，不使用真实业务库。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}", connect_args={"timeout": 20})
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Subscribe, SubscriptionSearchBatch, SubscriptionSearchTask, SearchSession,
    )])
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def _identity(media_type="电影", group=None):
    """
    构造明确包含媒体类型与剧集组的订阅身份。
    """
    return {"media_source": "themoviedb", "media_id": "123", "type": media_type,
            "season": 1, "episode_group": group}


def _payload(media_type="电影", group=None):
    """
    构造同一用户名的订阅写入字段。
    """
    return {**_identity(media_type, group), "name": "示例", "username": "alice"}


def test_empty_owner_keeps_legacy_duplicates_inside_caller_transaction(database):
    """
    旧空用户名不占唯一身份，允许重复暂存且全部随调用方回滚。
    """
    with database() as session:
        oper = SubscribeOper(session)
        payload = {**_payload(), "username": ""}
        first = oper.stage_add(_identity(), payload, "")
        second = oper.stage_add(_identity(), payload, "")
        assert first.created and second.created
        assert first.subscribe_id != second.subscribe_id
        assert session.scalar(select(func.count()).select_from(Subscribe)) == 2
        assert all(row.identity_key is None for row in session.scalars(select(Subscribe)))
        session.rollback()
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Subscribe)) == 0


def test_async_empty_owner_keeps_legacy_duplicates_inside_caller_transaction(db):
    """
    正式AsyncSession沿用空用户名兼容语义，不提前提交两条无唯一身份的记录。
    """
    db.watermark(Subscribe)

    async def execute(session):
        """
        在独立异步事务中暂存两个旧身份，再由调用方撤销。
        """
        oper = SubscribeOper(session)
        payload = {**_payload(), "username": ""}
        first = await oper.async_stage_add(_identity(), payload, "")
        second = await oper.async_stage_add(_identity(), payload, "")
        assert first.created and second.created
        assert first.subscribe_id != second.subscribe_id
        rows = list((await session.scalars(select(Subscribe).where(
            Subscribe.id.in_([first.subscribe_id, second.subscribe_id]),
        ))).all())
        assert len(rows) == 2 and all(row.identity_key is None for row in rows)
        await session.rollback()
        assert await session.get(Subscribe, first.subscribe_id) is None
        assert await session.get(Subscribe, second.subscribe_id) is None

    db.run_async_session(execute)


def test_movie_and_tv_with_same_source_number_are_distinct(database):
    """
    同一来源的电影和电视剧编号相同也能分别订阅。
    """
    with database() as session:
        oper = SubscribeOper(session)
        movie = oper.stage_add(_identity(), _payload(), "alice")
        tv = oper.stage_add(_identity("电视剧"), _payload("电视剧"), "alice")
        session.commit()
    assert movie.subscribe_id != tv.subscribe_id
    assert movie.created and tv.created


@pytest.mark.asyncio
async def test_async_expired_identity_fields_cannot_bypass_unique_target(tmp_path):
    """
    异步会话的过期模型更新也不能把缺少加载字段误判成无身份记录。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'expired-identity.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Subscribe.__table__.create)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            existing = Subscribe(**_payload())
            changing = Subscribe(**{**_payload(), "media_id": "456"})
            session.add_all([existing, changing])
            await session.commit()
            session.expire(changing, ["username", "type", "media_source", "season", "episode_group", "music_type"])
            changing.media_id = "123"
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(Subscribe)) == 2
            assert {record.media_id for record in await session.scalars(select(Subscribe))} == {"123", "456"}
    finally:
        await engine.dispose()


def test_source_lookup_keeps_episode_group(database):
    """
    同一作品同季的不同剧集组定位到各自记录。
    """
    with database() as session:
        first = Subscribe(**_payload("电视剧", "first"))
        second = Subscribe(**_payload("电视剧", "second"))
        session.add_all([first, second])
        session.commit()
        query = SubscriptionQueryService(SessionSubscriptionRepository(session))
        record = query.get_by_source(_identity("电视剧", "second"))
        assert record.id == second.id


@pytest.mark.asyncio
async def test_async_source_lookup_keeps_episode_group(tmp_path):
    """
    请求级异步来源查找也须精确匹配剧集组，不能读到默认组或首条记录。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'groups.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Subscribe.__table__.create)
        async with factory() as session:
            first = Subscribe(**_payload("电视剧", "first"))
            second = Subscribe(**_payload("电视剧", "second"))
            session.add_all([first, second])
            await session.commit()
            record = await Subscribe.async_get_by(
                session, type="电视剧", media_source="themoviedb", media_id="123", season=1,
                episode_group="second",
            )
            assert record.id == second.id
    finally:
        await engine.dispose()


def test_concurrent_creation_has_one_identity_owner(database, monkeypatch):
    """
    两个并发事务对同一用户同一身份只创建一行并返回相同主键。
    """
    rendezvous = Barrier(2)
    original_exists = SubscribeOper._exists

    def simultaneous_lookup(instance, *args, **kwargs):
        """
        强制两个事务都在对方插入前完成空结果读取。
        """
        result = original_exists(instance, *args, **kwargs)
        rendezvous.wait(timeout=10)
        return result

    monkeypatch.setattr(SubscribeOper, "_exists", simultaneous_lookup)

    def create(_index):
        """
        在独占 Session 中执行真实暂存和提交。
        """
        with database() as session:
            result = SubscribeOper(session).stage_add(_identity(), _payload(), "alice")
            session.commit()
            return result.subscribe_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    with database() as session:
        assert len(list(session.scalars(select(Subscribe)))) == 1
    assert ids[0] == ids[1]


@pytest.mark.asyncio
async def test_async_concurrent_creation_uses_same_database_identity(tmp_path, monkeypatch):
    """
    请求级 AsyncSession 并发新增也由同一数据库唯一键仲裁。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Subscribe.__table__.create)
    ready = asyncio.Event()
    count = 0
    original = SubscribeOper._async_exists

    async def lookup(instance, *args, **kwargs):
        """
        固定两个请求均先查重，再一起进入插入仲裁。
        """
        nonlocal count
        record = await original(instance, *args, **kwargs)
        count += 1
        if count == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return record

    monkeypatch.setattr(SubscribeOper, "_async_exists", lookup)

    async def create():
        """
        使用独占异步会话创建订阅并提交。
        """
        async with factory() as session:
            result = await SubscribeOper(session).async_stage_add(_identity(), _payload(), "alice")
            await session.commit()
            return result.subscribe_id

    try:
        ids = await asyncio.gather(create(), create())
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Subscribe)) == 1
        assert ids[0] == ids[1]
    finally:
        await engine.dispose()


def test_conflicting_retarget_rolls_back_old_progress(database):
    """
    改成同用户已有身份时明确拒绝，旧记录与另一目标的下载事实均不变。
    """
    with database() as session:
        first = Subscribe(**_payload("电视剧"), note=[1], episode_priority={"1": 100})
        second = Subscribe(**{**_payload("电视剧"), "media_id": "456"}, note=[2])
        session.add_all([first, second])
        session.commit()
        first_id, second_id = first.id, second.id
        service = SyncSubscriptionMutationService(
            repository=SessionSubscriptionRepository(session), unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=Mock(), dispatch_store=Mock(), publish_modified=Mock(),
        )
        with pytest.raises(SubscriptionIdentityConflictError):
            service.update(first_id, {"media_id": "456"}, SubscriptionActor("alice", False))
        assert session.get(Subscribe, first_id).media_id == "123"
        assert session.get(Subscribe, first_id).note == [1]
        assert session.get(Subscribe, second_id).note == [2]


@pytest.mark.parametrize("expired_fields", [None, ["username", "type", "media_source", "season", "episode_group", "music_type"]])
def test_expired_orm_identity_still_rejects_conflicting_retarget(database, expired_fields):
    """
    ORM 已过期属性不能让直接兼容写入跳过唯一身份约束。
    """
    with database() as session:
        first = Subscribe(**_payload("电视剧"))
        second = Subscribe(**{**_payload("电视剧"), "media_id": "456"})
        session.add_all([first, second])
        session.commit()
        first_id = first.id
        session.expire(first, expired_fields)
        first.media_id = "456"
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        assert session.get(Subscribe, first_id).media_id == "123"


def test_expired_cancelled_search_releases_active_key(database):
    """
    旧进程取消中的租约过期后应终结任务，允许同订阅重新入队。
    """
    repository = TransactionalSubscriptionSearchRepository(database)
    batch = repository.enqueue(subscription_ids=(7,), source="manual", priority=100)
    claimed = repository.claim_next(owner="lost-process")
    assert repository.request_cancel(batch.batch.batch_id)
    with database() as session:
        session.execute(update(SubscriptionSearchTask).where(
            SubscriptionSearchTask.task_id == claimed.task_id,
        ).values(lease_expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
        session.commit()
    assert repository.claim_next(owner="new-process") is None
    fresh = repository.enqueue(subscription_ids=(7,), source="manual", priority=100)
    assert fresh.created_count == 1
    assert repository.claim_next(owner="new-process").task_id != claimed.task_id


@pytest.mark.parametrize("changed", [{"media_id": "456"}, {"season": 2}, {"episode_group": "new"}])
def test_retarget_clears_old_episode_progress(changed):
    """
    作品、季或剧集组变化不继承旧目标的下载进度和洗版优先级。
    """
    snapshot = SubscriptionSnapshot(id=7, name="旧作品", type="电视剧", media_source=MediaSource.TMDB,
        media_id="123", season=1, note=[1], episode_priority={"1": 100}, current_priority=100,
        total_episode=12, lack_episode=11)
    values = _normalized_subscription_patch(snapshot, changed).to_payload()
    assert values["note"] == []
    assert values["episode_priority"] == {}
    assert values["current_priority"] is None
    assert values["lack_episode"] == 12


@pytest.mark.asyncio
async def test_servarr_duplicate_series_returns_existing_id():
    """
    重复添加的兼容接口返回实际记录 ID，后续读取和删除不会误用 1。
    """
    record = SimpleNamespace(id=47, name="示例", year="2026", type="电视剧", season=1,
                             poster=None, media_source="themoviedb", media_id="123")
    repository = Mock(async_exists=AsyncMock(return_value=record))
    service = ServarrSubscriptionService(async_repository=repository, sync_repository=Mock())
    result = await arr_add_series(
        SonarrSeries(tmdbId=123, title="示例", seasons=[SonarrSeason(seasonNumber=1, monitored=True)]),
        "fake-api-key", service, Mock(),
    )
    assert result.id == 47
