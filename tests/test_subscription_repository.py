"""订阅仓储的短 Session 投影与请求事务所有权测试。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.subscription.complete import CompleteSubscriptionCommand
from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
)
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
    TransactionalSubscriptionRepository,
)
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.types import MediaSource, MediaType


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
            history_repository=SessionSubscriptionHistoryRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
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
            history_repository=SessionSubscriptionHistoryRepository(session),
            unit_of_work=_FailingUnitOfWork(session),
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            await service.delete_history(
                history_id,
                SubscriptionActor(name="alice", is_superuser=False),
            )

    db.run_async_session(delete_history)
    db.session.expire_all()
    assert db.session.get(SubscribeHistory, history_id) is not None
