"""订阅写入端口的 SQLAlchemy 事务适配器。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxRepository,
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
            outbox = SqlAlchemyOutboxRepository(session)
            command = CreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )

            def delivered(subscribe_id: int) -> None:
                """执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    report_delivered = after_commit(subscribe_id)
                    outbox.complete_by_event_key(
                        subscription_added_event_key(subscribe_id, payload),
                        datetime.now(timezone.utc),
                    )
                    if notification:
                        outbox.complete_by_event_key(
                            subscription_added_notification_key(subscribe_id, payload),
                            datetime.now(timezone.utc),
                        )
                    if report_delivered is not False:
                        outbox.complete_by_event_key(
                            subscription_added_report_key(subscribe_id, payload),
                            datetime.now(timezone.utc),
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
            command = AsyncCreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                outbox=outbox,
            )

            async def delivered(subscribe_id: int) -> None:
                """异步执行提交后编排，分别收口已确认的 durable intent。"""
                if after_commit:
                    report_delivered = await after_commit(subscribe_id)
                    await outbox.complete_by_event_key(
                        subscription_added_event_key(subscribe_id, payload),
                        datetime.now(timezone.utc),
                    )
                    if notification:
                        await outbox.complete_by_event_key(
                            subscription_added_notification_key(subscribe_id, payload),
                            datetime.now(timezone.utc),
                        )
                    if report_delivered is not False:
                        await outbox.complete_by_event_key(
                            subscription_added_report_key(subscribe_id, payload),
                            datetime.now(timezone.utc),
                        )

            return await command.execute(
                identity,
                payload,
                username,
                delivered,
                notification,
            )
