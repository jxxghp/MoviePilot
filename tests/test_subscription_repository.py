"""订阅仓储的短 Session 投影与请求事务所有权测试。"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.subscription.complete import CompleteSubscriptionCommand
from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
    SyncSubscriptionMutationService,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
    TransactionalSubscriptionRepository,
)
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.types import MediaSource, MediaType


async def _ignore_async_modified(_payload: dict) -> None:
    """为不触发修改事件的测试提供完整异步发布端口。"""


def _ignore_sync_modified(_payload: dict) -> None:
    """为不触发修改事件的测试提供完整同步发布端口。"""


def test_transactional_repository_returns_frozen_detached_snapshot(db) -> None:
    """standalone 查询在 Session 内复制 JSON，返回值不携带 ORM 生命周期。"""
    row = db.add(
        Subscribe(
            name="短会话订阅",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-snapshot-1",
            sites=[1, 2],
            episode_priority={"1": 80},
            filter_groups=["WEB-DL"],
        )
    )
    repository = TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )

    snapshot = repository.get(row.id)

    assert snapshot is not None
    assert snapshot.id == row.id
    assert snapshot.media_source == MediaSource.TMDB
    assert snapshot.sites == [1, 2]
    assert snapshot.episode_priority == {"1": 80}
    with pytest.raises(TypeError, match="不可修改"):
        snapshot.sites.append(3)  # type: ignore[union-attr]
    with pytest.raises(TypeError, match="不可修改"):
        snapshot.episode_priority["1"] = 100  # type: ignore[index]


def test_session_repository_leaves_commit_and_rollback_to_caller(db) -> None:
    """请求级仓储只 flush 暂存记录，调用方回滚后数据库不保留该记录。"""
    db.watermark(Subscribe)

    async def stage_and_rollback(session: AsyncSession) -> int:
        """在同一请求 Session 暂存、读取并由调用方回滚。"""
        repository = SessionSubscriptionRepository(session)
        result = await repository.async_stage_add(
            SubscriptionIdentity(
                media_source=MediaSource.TMDB,
                media_id="subscription-request-rollback",
                type=MediaType.TV.value,
            ),
            SubscriptionPatch(
                {
                    "name": "请求级订阅",
                    "type": MediaType.TV.value,
                    "media_source": MediaSource.TMDB.value,
                    "media_id": "subscription-request-rollback",
                }
            ),
        )
        snapshot = await repository.async_get(result.subscribe_id)
        assert snapshot is not None
        await session.rollback()
        return result.subscribe_id

    subscribe_id = db.run_async_session(stage_and_rollback)

    db.session.expire_all()
    assert Subscribe.get(db.session, subscribe_id) is None


def test_completion_writes_history_and_deletes_subscription_atomically(db) -> None:
    """完成命令在同一真实事务中写历史并删除活动订阅。"""
    db.watermark(Subscribe, SubscribeHistory)
    row = db.add(
        Subscribe(
            name="待完成订阅",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-complete-atomic",
            season=2,
            lack_episode=0,
            note=[1, 2],
            state="R",
            downloader="default",
            manual_total_episode=1,
        )
    )
    subscribe_id = row.id
    snapshot = TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    ).get(subscribe_id)
    assert snapshot is not None

    with SessionFactory() as session:
        command = CompleteSubscriptionCommand(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=None,
            dispatch_store=None,
            publish=lambda _payload: None,
        )
        command.execute(
            subscribe_id,
            snapshot.to_dict(),
            {"title": snapshot.name},
            notify=lambda: None,
            report=lambda _payload: True,
        )

    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id) is None
    history = db.session.query(SubscribeHistory).filter_by(media_id="subscription-complete-atomic").one()
    assert history.name == "待完成订阅"
    assert not hasattr(history, "lack_episode")


def test_delete_history_commits_request_transaction(db) -> None:
    """授权后的历史删除由 mutation 服务提交请求 Session。"""
    row = db.add(
        SubscribeHistory(
            name="待删除历史",
            type=MediaType.TV.value,
            username="alice",
        )
    )
    history_id = row.id

    async def delete_history(session: AsyncSession) -> bool:
        """使用真实 request adapter 和 UoW 删除历史。"""
        service = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            publish_modified=_ignore_async_modified,
            history_repository=SessionSubscriptionHistoryRepository(session),
        )
        return await service.delete_history(
            history_id,
            SubscriptionActor(name="alice", is_superuser=False),
        )

    assert db.run_async_session(delete_history) is True
    db.session.expire_all()
    assert db.session.get(SubscribeHistory, history_id) is None


def test_delete_history_rolls_back_when_commit_fails(db) -> None:
    """历史删除提交异常时回滚，同一真实 Session 中的删除不会泄漏。"""
    row = db.add(
        SubscribeHistory(
            name="回滚历史",
            type=MediaType.TV.value,
            username="alice",
        )
    )
    history_id = row.id

    class _FailingUnitOfWork:
        """提交失败并用真实 AsyncSession 执行回滚。"""

        def __init__(self, session: AsyncSession) -> None:
            """保存待回滚的请求 Session。"""
            self._session = session

        async def commit(self) -> None:
            """模拟数据库提交失败。"""
            raise RuntimeError("commit failed")

        async def rollback(self) -> None:
            """回滚同一请求 Session。"""
            await self._session.rollback()

    async def delete_history(session: AsyncSession) -> None:
        """执行会在提交阶段失败的真实历史删除。"""
        service = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=_FailingUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            publish_modified=_ignore_async_modified,
            history_repository=SessionSubscriptionHistoryRepository(session),
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            await service.delete_history(
                history_id,
                SubscriptionActor(name="alice", is_superuser=False),
            )

    db.run_async_session(delete_history)
    db.session.expire_all()
    assert db.session.get(SubscribeHistory, history_id) is not None


def test_async_mutation_commits_row_and_outbox_atomically(db) -> None:
    """异步修改把订阅行与 outbox intent 一次提交，发布成功后再完成 intent。"""
    db.watermark(Subscribe, OutboxMessage)
    row = db.add(
        Subscribe(
            name="异步修改前",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-async-mutation-commit",
            username="alice",
        )
    )
    published = []

    async def execute(session: AsyncSession):
        """在真实异步请求 Session 中执行一次完整修改。"""
        async def publish(payload: dict) -> None:
            """记录提交后收到的 durable 事件快照。"""
            published.append(payload)

        service = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            publish_modified=publish,
        )
        return await service.update(
            row.id,
            {"name": "异步修改后"},
            SubscriptionActor(name="alice", is_superuser=False),
        )

    change = db.run_async_session(execute)

    db.session.expire_all()
    assert change is not None and change.business_committed and change.event_published
    assert change.snapshot.name == "异步修改后"
    assert db.session.get(Subscribe, row.id).name == "异步修改后"
    intent = db.session.query(OutboxMessage).filter_by(
        event_key=published[0]["idempotency_key"]
    ).one()
    assert intent.status == "completed"
    assert intent.payload["subscribe_info"]["name"] == "异步修改后"


def test_async_mutation_rolls_back_row_and_outbox_together(db) -> None:
    """异步 intent 暂存失败时，已 flush 的订阅行和 intent 都必须回滚。"""
    db.watermark(Subscribe, OutboxMessage)
    intent_watermark = db.session.execute(
        select(func.max(OutboxMessage.id))
    ).scalar() or 0
    row = db.add(
        Subscribe(
            name="异步回滚前",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-async-mutation-rollback",
            username="alice",
        )
    )

    class _FailingStager:
        """真实暂存 intent 后抛错，验证整个业务事务回滚。"""

        def __init__(self, session: AsyncSession) -> None:
            """绑定与订阅行相同的异步 Session。"""
            self._stager = SqlAlchemyAsyncOutboxStager(session)

        async def stage(self, intent, now) -> None:
            """先 flush intent，再模拟事务内后续失败。"""
            await self._stager.stage(intent, now)
            raise RuntimeError("outbox stage failed")

    async def execute(session: AsyncSession) -> None:
        """执行预期回滚的异步修改。"""
        service = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=_FailingStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            publish_modified=_ignore_async_modified,
        )
        with pytest.raises(RuntimeError, match="outbox stage failed"):
            await service.update(
                row.id,
                {"name": "不应提交"},
                SubscriptionActor(name="alice", is_superuser=False),
            )

    db.run_async_session(execute)

    db.session.expire_all()
    assert db.session.get(Subscribe, row.id).name == "异步回滚前"
    assert not any(
        intent.payload.get("subscribe_id") == row.id
        for intent in db.session.query(OutboxMessage)
        .filter(OutboxMessage.id > intent_watermark)
        .all()
    )


def test_async_pending_intent_is_not_published_outside_outbox(db) -> None:
    """即时认领失败时保留 pending，服务不得绕过 outbox 调用 publisher。"""
    db.watermark(Subscribe, OutboxMessage)
    row = db.add(
        Subscribe(
            name="待派发修改前",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-async-mutation-pending",
            username="alice",
        )
    )
    published = []

    class _UnavailableStore:
        """模拟 intent 已提交但当前未取得即时派发 lease。"""

        async def claim_by_event_key(self, _event_key, _now, _lease_until):
            """拒绝当前即时认领。"""
            return None

        async def complete(self, *_args, **_kwargs) -> bool:
            """未认领时禁止完成。"""
            raise AssertionError("pending intent 不得完成")

        async def retry(self, *_args, **_kwargs) -> bool:
            """未认领时禁止释放。"""
            raise AssertionError("pending intent 不得重试")

    async def execute(session: AsyncSession):
        """提交后返回 pending 状态。"""
        async def publish(payload: dict) -> None:
            """记录任何越过 outbox 的错误发布。"""
            published.append(payload)

        service = SubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=_UnavailableStore(),
            publish_modified=publish,
        )
        return await service.update(
            row.id,
            {"name": "待 dispatcher 派发"},
            SubscriptionActor(name="alice", is_superuser=False),
        )

    change = db.run_async_session(execute)

    db.session.expire_all()
    assert change is not None and change.business_committed
    assert change.snapshot.name == "待 dispatcher 派发"
    assert not change.event_published and len(change.pending_effects) == 1
    assert published == []
    intent = db.session.query(OutboxMessage).filter_by(
        event_key=change.pending_effects[0]
    ).one()
    assert intent.status == "pending"
    assert db.session.get(Subscribe, row.id).name == "待 dispatcher 派发"


def test_sync_mutation_commits_row_and_outbox_atomically(db) -> None:
    """同步修改与异步服务共享同一行、intent、提交后派发语义。"""
    db.watermark(Subscribe, OutboxMessage)
    row = db.add(
        Subscribe(
            name="同步修改前",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-sync-mutation-commit",
            username="alice",
        )
    )
    published = []
    with SessionFactory() as session:
        service = SyncSubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=SqlAlchemyOutboxStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
            publish_modified=published.append,
        )
        change = service.update(
            row.id,
            {"name": "同步修改后"},
            SubscriptionActor(name="alice", is_superuser=False),
        )

    db.session.expire_all()
    assert change is not None and change.business_committed and change.event_published
    assert change.snapshot.name == "同步修改后"
    assert db.session.get(Subscribe, row.id).name == "同步修改后"
    intent = db.session.query(OutboxMessage).filter_by(
        event_key=published[0]["idempotency_key"]
    ).one()
    assert intent.status == "completed"


def test_sync_mutation_rolls_back_row_and_outbox_together(db) -> None:
    """同步 intent 暂存失败时回滚同行更新和已 flush 的 intent。"""
    db.watermark(Subscribe, OutboxMessage)
    intent_watermark = db.session.execute(
        select(func.max(OutboxMessage.id))
    ).scalar() or 0
    row = db.add(
        Subscribe(
            name="同步回滚前",
            type=MediaType.TV.value,
            media_source=MediaSource.TMDB.value,
            media_id="subscription-sync-mutation-rollback",
            username="alice",
        )
    )

    class _FailingStager:
        """同步暂存 intent 后模拟事务内异常。"""

        def __init__(self, session) -> None:
            """绑定与订阅行相同的同步 Session。"""
            self._stager = SqlAlchemyOutboxStager(session)

        def stage(self, intent, now) -> None:
            """先 flush intent，再中断业务事务。"""
            self._stager.stage(intent, now)
            raise RuntimeError("sync outbox stage failed")

    with SessionFactory() as session:
        service = SyncSubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=_FailingStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
            publish_modified=_ignore_sync_modified,
        )
        with pytest.raises(RuntimeError, match="sync outbox stage failed"):
            service.update(
                row.id,
                {"name": "不应提交"},
                SubscriptionActor(name="alice", is_superuser=False),
            )

    db.session.expire_all()
    assert db.session.get(Subscribe, row.id).name == "同步回滚前"
    assert not any(
        intent.payload.get("subscribe_id") == row.id
        for intent in db.session.query(OutboxMessage)
        .filter(OutboxMessage.id > intent_watermark)
        .all()
    )
