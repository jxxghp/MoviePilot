"""
订阅活动短事务持久化与权限隔离适配器。
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.activity import SubscriptionActivity
from app.db.models.subscribeactivity import SubscribeActivity
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribeactivity import SubscribeActivityOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.uow import SqlAlchemyUnitOfWork


def project_activity(row: SubscribeActivity) -> SubscriptionActivity:
    """
    在会话关闭前复制业务事件及其 JSON 明细。
    """
    return SubscriptionActivity(**{key: getattr(row, key) for key in SubscriptionActivity.__dataclass_fields__})


class TransactionalSubscriptionActivityRepository:
    """
    后台写入和 API 分页各使用独立短会话，不持有长执行事务。
    """

    def __init__(self, sync_session: Callable[[], AbstractContextManager[Session]],
                 async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        """
        注入宿主数据库会话作用域。
        """
        self._sync_session = sync_session
        self._async_session = async_session

    def save(self, event: SubscriptionActivity) -> None:
        """
        原子保存单轮快照，失败由调用方决定是否影响业务。
        """
        with self._sync_session() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                SubscribeActivityOper(session).save(asdict(event))
                unit_of_work.commit()
            except Exception:
                unit_of_work.rollback()
                raise

    def get(self, activity_key: str, event_id: str) -> SubscriptionActivity | None:
        """
        恢复同一任务轮次的安全进度，关闭会话后返回独立快照。
        """
        with self._sync_session() as session:
            row = SubscribeActivityOper(session).get(activity_key, event_id)
            return project_activity(row) if row is not None else None

    async def list_accessible(self, subscription_id: int, username: str, superuser: bool, *,
                              history: bool, kind: str | None, since: str | None,
                              before: str | None, limit: int) -> tuple[list[SubscriptionActivity], str | None] | None:
        """
        仅 owner 或管理员可读取，历史与活动整数 ID 分开解释。
        """
        async with self._async_session() as session:
            repository = SubscribeHistoryOper(session) if history else SubscribeOper(session)
            subscribe = await repository.async_get(subscription_id)
            if subscribe is None or (not superuser and subscribe.username != username):
                return None
            if not subscribe.activity_key:
                return [], None
            rows = await SubscribeActivityOper(session).page(
                subscribe.activity_key, kind=kind, since=since, before=before, limit=limit + 1,
            )
            return [project_activity(row) for row in rows[:limit]], rows[limit - 1].event_id if len(rows) > limit else None

    async def get_accessible(self, subscription_id: int, event_id: str, username: str,
                             superuser: bool, *, history: bool) -> SubscriptionActivity | None:
        """
        事件 ID 不能绕过活动或历史订阅的当前归属检查。
        """
        async with self._async_session() as session:
            repository = SubscribeHistoryOper(session) if history else SubscribeOper(session)
            subscribe = await repository.async_get(subscription_id)
            if subscribe is None or not subscribe.activity_key or (not superuser and subscribe.username != username):
                return None
            row = await SubscribeActivityOper(session).async_get(subscribe.activity_key, event_id)
            return project_activity(row) if row is not None else None
