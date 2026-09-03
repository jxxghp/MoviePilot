"""订阅执行状态的请求级异步 SQLAlchemy 适配器。"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.subscription.execution import SearchBatchSnapshot, SearchTaskSnapshot
from app.db.models.subscriptionsearch import SubscriptionSearchBatch, SubscriptionSearchTask


def _task(record: SubscriptionSearchTask) -> SearchTaskSnapshot:
    """复制可脱离 AsyncSession 使用的搜索任务快照。"""
    return SearchTaskSnapshot(
        task_id=record.task_id,
        batch_id=record.batch_id,
        subscription_id=record.subscription_id,
        source=record.source,
        priority=record.priority,
        position=record.position,
        state=record.state,
        phase=record.phase,
        attempt_count=record.attempt_count,
        cancel_requested=bool(record.cancel_requested),
        lease_token=record.lease_token,
        created_at=record.created_at,
        updated_at=record.updated_at,
        available_at=record.available_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
        current_site_id=record.current_site_id,
    )


def _batch(record: SubscriptionSearchBatch) -> SearchBatchSnapshot:
    """复制可脱离 AsyncSession 使用的搜索批次快照。"""
    return SearchBatchSnapshot(
        batch_id=record.batch_id,
        source=record.source,
        state=record.state,
        priority=record.priority,
        total_count=record.total_count,
        finished_count=record.finished_count,
        failed_count=record.failed_count,
        cancelled_count=record.cancelled_count,
        skipped_count=record.skipped_count,
        cancel_requested=bool(record.cancel_requested),
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
    )


class SessionSubscriptionExecutionStatusRepository:
    """复用请求 AsyncSession 批量读取搜索执行事实。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定请求持有的异步会话。"""
        self._session = session

    async def latest_search_tasks(
        self,
        subscription_ids: tuple[int, ...],
    ) -> dict[int, SearchTaskSnapshot]:
        """按更新时间倒序读取并在内存中保留每条订阅首项。"""
        result = await self._session.execute(
            select(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.subscription_id.in_(subscription_ids))
            .order_by(
                SubscriptionSearchTask.updated_at.desc(),
                SubscriptionSearchTask.id.desc(),
            )
        )
        snapshots: dict[int, SearchTaskSnapshot] = {}
        for record in result.scalars().all():
            snapshots.setdefault(record.subscription_id, _task(record))
        return snapshots

    async def list_batches(self, *, limit: int) -> list[SearchBatchSnapshot]:
        """返回最近更新的批次，访问范围由应用服务依据任务校验。"""
        result = await self._session.execute(
            select(SubscriptionSearchBatch)
            .order_by(
                SubscriptionSearchBatch.updated_at.desc(),
                SubscriptionSearchBatch.id.desc(),
            )
            .limit(limit)
        )
        return [_batch(record) for record in result.scalars().all()]

    async def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """按稳定批次 ID 返回状态快照。"""
        result = await self._session.execute(
            select(SubscriptionSearchBatch).where(
                SubscriptionSearchBatch.batch_id == batch_id
            )
        )
        record = result.scalars().first()
        return _batch(record) if record else None

    async def list_batch_tasks(self, batch_id: str) -> list[SearchTaskSnapshot]:
        """按持久位置返回批次任务，供访问校验和当前阶段投影。"""
        result = await self._session.execute(
            select(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.batch_id == batch_id)
            .order_by(SubscriptionSearchTask.position, SubscriptionSearchTask.id)
        )
        return [_task(record) for record in result.scalars().all()]
