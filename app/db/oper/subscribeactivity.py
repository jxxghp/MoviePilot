"""
复用调用方会话的订阅业务活动读写原语。
"""

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models.subscribeactivity import SubscribeActivity


class SubscribeActivityOper:
    """
    不自行提交事务的订阅活动存取器。
    """

    def __init__(self, session: Session | AsyncSession) -> None:
        """
        绑定调用方事务拥有的数据库会话。
        """
        self.session = session

    def stage(self, payload: dict[str, Any]) -> None:
        """
        暂存生命周期事实，提交或回滚由订阅用例统一负责。
        """
        self.session.add(SubscribeActivity(**payload))

    def save(self, payload: dict[str, Any]) -> None:
        """
        同步暂存轮次快照，固定事件 ID 的重试保持一条活动。
        """
        if not isinstance(self.session, Session):
            raise RuntimeError("轮次快照需要同步会话")
        row = self.session.execute(select(SubscribeActivity).where(SubscribeActivity.event_id == payload["event_id"])).scalar_one_or_none()
        if row is None:
            self.stage(payload)
        elif row.activity_key == payload["activity_key"]:
            for key, value in payload.items():
                setattr(row, key, value)

    def get(self, activity_key: str, event_id: str) -> SubscribeActivity | None:
        """
        在当前同步会话内读取指定订阅拥有的轮次。
        """
        if not isinstance(self.session, Session):
            raise RuntimeError("读取轮次进度需要同步会话")
        return cast(SubscribeActivity | None, self.session.execute(select(SubscribeActivity).where(
            SubscribeActivity.activity_key == activity_key, SubscribeActivity.event_id == event_id,
        )).scalar_one_or_none())

    def get_event(self, event_id: str) -> SubscribeActivity | None:
        """
        按唯一事件身份读取队列对应的活动，仅用于数据库内部结算。
        """
        if not isinstance(self.session, Session):
            raise RuntimeError("读取活动事件需要同步会话")
        return cast(SubscribeActivity | None, self.session.execute(select(SubscribeActivity).where(SubscribeActivity.event_id == event_id)).scalar_one_or_none())

    async def page(self, activity_key: str, *, kind: str | None, since: str | None,
                   before: str | None, limit: int) -> list[SubscribeActivity]:
        """
        异步按不可复用的递增行游标返回活动，避免并发新增导致翻页重复。
        """
        if not isinstance(self.session, AsyncSession):
            raise RuntimeError("活动分页需要异步会话")
        statement = select(SubscribeActivity).where(SubscribeActivity.activity_key == activity_key)
        if kind:
            statement = statement.where(SubscribeActivity.kind == kind)
        if since:
            statement = statement.where(SubscribeActivity.started_at >= since)
        if before is not None:
            cursor = select(SubscribeActivity.id).where(SubscribeActivity.activity_key == activity_key, SubscribeActivity.event_id == before).scalar_subquery()
            statement = statement.where(SubscribeActivity.id < cursor)
        rows = (await self.session.execute(statement.order_by(SubscribeActivity.id.desc()).limit(limit))).scalars().all()
        return list(rows)

    async def async_get(self, activity_key: str, event_id: str) -> SubscribeActivity | None:
        """
        在异步会话内读取同订阅拥有的完整活动。
        """
        if not isinstance(self.session, AsyncSession):
            raise RuntimeError("活动详情需要异步会话")
        return cast(SubscribeActivity | None, (await self.session.execute(select(SubscribeActivity).where(
            SubscribeActivity.activity_key == activity_key, SubscribeActivity.event_id == event_id,
        ))).scalar_one_or_none())
