"""订阅写入端口的 SQLAlchemy 事务适配器。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.outbox import (
    OUTBOX_LEASE_SECONDS,
    AsyncOutboxDispatchStore,
    ClaimedOutboxMessage,
    OutboxDispatchStore,
    OutboxLeaseLostError,
)
from app.application.subscription.write import (
    AfterCommitEffect,
    AsyncAfterCommitEffect,
    AsyncCreateSubscriptionCommand,
    CreateSubscriptionCommand,
    subscription_added_event_key,
    subscription_added_notification_key,
    subscription_added_report_key,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.oper.subscribe import SubscribeOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork


class TransactionalSubscribeWriter:
    """为每次订阅新增创建独占会话，并把提交权交给 Application Command。"""

    def __init__(
        self,
        sync_session: Callable[[], Session],
        async_session: Callable[
            [],
            AbstractAsyncContextManager[AsyncSession],
        ],
    ) -> None:
        """注入同步会话工厂和异步会话作用域。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def add(
        self,
        identity: dict[str, Any],
        payload: dict[str, Any],
        username: str | None = None,
        after_commit: AfterCommitEffect | None = None,
        notification: dict[str, object] | None = None,
    ) -> tuple[int, str]:
        """在独占同步会话内执行一次完整订阅新增事务。"""
        session = self._sync_session()
        try:
            outbox = SqlAlchemyOutboxStager(session)
            dispatch_store = SqlAlchemyOutboxDispatchStore(self._sync_session)
            command = CreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )

            def delivered(subscribe_id: int) -> None:
                """执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    _deliver_added_effects(
                        dispatch_store,
                        subscribe_id,
                        payload,
                        notification,
                        lambda: after_commit(subscribe_id),
                    )

            return command.execute(
                identity,
                payload,
                username,
                delivered,
                notification,
            )
        finally:
            session.close()

    async def async_add(
        self,
        identity: dict[str, Any],
        payload: dict[str, Any],
        username: str | None = None,
        after_commit: AsyncAfterCommitEffect | None = None,
        notification: dict[str, object] | None = None,
    ) -> tuple[int, str]:
        """在独占异步会话作用域内执行一次完整订阅新增事务。"""
        async with self._async_session() as session:
            outbox = SqlAlchemyAsyncOutboxStager(session)
            dispatch_store = SqlAlchemyAsyncOutboxDispatchStore(self._async_session)
            command = AsyncCreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                outbox=outbox,
            )

            async def delivered(subscribe_id: int) -> None:
                """异步执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    await _deliver_added_effects_async(
                        dispatch_store,
                        subscribe_id,
                        payload,
                        notification,
                        lambda: after_commit(subscribe_id),
                    )

            return await command.execute(
                identity,
                payload,
                username,
                delivered,
                notification,
            )


def _added_effect_keys(
    subscribe_id: int,
    payload: dict[str, Any],
    notification: Optional[dict[str, object]],
) -> tuple[str, ...]:
    """返回组合回调实际包含的独立 durable effect 键。"""
    keys = [subscription_added_event_key(subscribe_id, payload)]
    if notification:
        keys.append(subscription_added_notification_key(subscribe_id, payload))
    keys.append(subscription_added_report_key(subscribe_id, payload))
    return tuple(keys)


def _claim_added_effects(
    store: OutboxDispatchStore,
    keys: tuple[str, ...],
    now: datetime,
) -> Optional[tuple[ClaimedOutboxMessage, ...]]:
    """全量认领组合回调；竞争丢失时释放本次已取得的 lease。"""
    claimed: list[ClaimedOutboxMessage] = []
    for key in keys:
        message = store.claim_by_event_key(
            key,
            now,
            now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
        )
        if message is None:
            for owned in claimed:
                store.retry(
                    owned.message_id,
                    owned.attempt,
                    next_retry_at=now,
                    last_error="组合副作用由其他 owner 接管",
                    dead=False,
                )
            return None
        claimed.append(message)
    return tuple(claimed)


def _deliver_added_effects(
    store: OutboxDispatchStore,
    subscribe_id: int,
    payload: dict[str, Any],
    notification: Optional[dict[str, object]],
    effect: Callable[[], Optional[bool]],
) -> None:
    """认领组合回调并按事件、通知、统计的确认结果分别结算。"""
    now = datetime.now(timezone.utc)
    claimed = _claim_added_effects(
        store,
        _added_effect_keys(subscribe_id, payload, notification),
        now,
    )
    if claimed is None:
        return
    try:
        report_delivered = effect()
    except Exception as error:
        for message in claimed:
            store.retry(
                message.message_id,
                message.attempt,
                next_retry_at=now,
                last_error=str(error)[:4000],
                dead=False,
            )
        raise
    for message in claimed[:-1]:
        if not store.complete(message.message_id, message.attempt, now):
            raise OutboxLeaseLostError("订阅新增完成凭证已失效")
    report = claimed[-1]
    if report_delivered is False:
        store.retry(
            report.message_id,
            report.attempt,
            next_retry_at=now,
            last_error="订阅新增统计未确认",
            dead=False,
        )
    else:
        if not store.complete(report.message_id, report.attempt, now):
            raise OutboxLeaseLostError("订阅新增统计完成凭证已失效")


async def _claim_added_effects_async(
    store: AsyncOutboxDispatchStore,
    keys: tuple[str, ...],
    now: datetime,
) -> Optional[tuple[ClaimedOutboxMessage, ...]]:
    """异步全量认领组合回调，竞争丢失时释放已取得 lease。"""
    claimed: list[ClaimedOutboxMessage] = []
    for key in keys:
        message = await store.claim_by_event_key(
            key,
            now,
            now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
        )
        if message is None:
            for owned in claimed:
                await store.retry(
                    owned.message_id,
                    owned.attempt,
                    next_retry_at=now,
                    last_error="组合副作用由其他 owner 接管",
                    dead=False,
                )
            return None
        claimed.append(message)
    return tuple(claimed)


async def _deliver_added_effects_async(
    store: AsyncOutboxDispatchStore,
    subscribe_id: int,
    payload: dict[str, Any],
    notification: Optional[dict[str, object]],
    effect: Callable[[], Any],
) -> None:
    """异步认领组合回调并按各 intent 的确认结果分别结算。"""
    now = datetime.now(timezone.utc)
    claimed = await _claim_added_effects_async(
        store,
        _added_effect_keys(subscribe_id, payload, notification),
        now,
    )
    if claimed is None:
        return
    try:
        report_delivered = await effect()
    except Exception as error:
        for message in claimed:
            await store.retry(
                message.message_id,
                message.attempt,
                next_retry_at=now,
                last_error=str(error)[:4000],
                dead=False,
            )
        raise
    for message in claimed[:-1]:
        if not await store.complete(message.message_id, message.attempt, now):
            raise OutboxLeaseLostError("订阅新增完成凭证已失效")
    report = claimed[-1]
    if report_delivered is False:
        await store.retry(
            report.message_id,
            report.attempt,
            next_retry_at=now,
            last_error="订阅新增统计未确认",
            dead=False,
        )
    else:
        if not await store.complete(report.message_id, report.attempt, now):
            raise OutboxLeaseLostError("订阅新增统计完成凭证已失效")
