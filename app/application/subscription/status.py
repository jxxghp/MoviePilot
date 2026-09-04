"""订阅执行状态的业务投影与访问范围治理。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional, Protocol

from app.application.subscription.execution import SearchBatchSnapshot, SearchTaskSnapshot


@dataclass(frozen=True, slots=True)
class SubscriptionExecutionStatus:
    """一个订阅最近搜索任务的用户可见状态。"""

    state: str
    phase: str
    updated_at: str
    source: Optional[str] = None
    batch_id: Optional[str] = None
    task_id: Optional[str] = None
    current_site_id: Optional[int] = None
    error: Optional[str] = None
    can_cancel: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionBatchStatus:
    """订阅搜索批次的进度、当前工作和操作能力。"""

    batch_id: str
    source: str
    state: str
    phase: str
    total_count: int
    processed_count: int
    finished_count: int
    failed_count: int
    cancelled_count: int
    created_at: str
    updated_at: str
    current_subscription_id: Optional[int] = None
    current_site_id: Optional[int] = None
    error: Optional[str] = None
    can_cancel: bool = False
    skipped_count: int = 0


class SubscriptionExecutionReadRepository(Protocol):
    """请求级读取搜索任务与批次事实的端口。"""

    async def latest_search_tasks(
        self,
        subscription_ids: tuple[int, ...],
    ) -> dict[int, SearchTaskSnapshot]:
        """返回每条订阅最近更新的搜索任务。"""
        ...

    async def list_batches(
        self,
        *,
        limit: int,
    ) -> list[SearchBatchSnapshot]:
        """返回最近更新的搜索批次。"""
        ...

    async def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """按稳定 ID 返回一个搜索批次。"""
        ...

    async def list_batch_tasks(self, batch_id: str) -> list[SearchTaskSnapshot]:
        """按稳定位置返回批次任务。"""
        ...


class SubscriptionExecutionStatusService:
    """把搜索队列投影为稳定业务状态。"""

    _ACTIVE_STATES = {
        "queued",
        "running",
        "matching",
        "searching",
        "waiting_site_budget",
        "preparing",
        "submitting",
        "cancelling",
    }
    def __init__(
        self,
        repository: SubscriptionExecutionReadRepository,
        request_cancel: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> None:
        """保存请求会话绑定的状态读取端口和可选取消用例。"""
        self._repository = repository
        self._request_cancel = request_cancel

    async def request_cancel(self, batch_id: str) -> bool:
        """通过组合根提供的执行边界请求取消搜索批次。"""
        if self._request_cancel is None:
            raise RuntimeError("订阅搜索批次取消能力未注册")
        return bool(await self._request_cancel(batch_id))

    async def for_subscriptions(
        self,
        subscription_ids: tuple[int, ...],
    ) -> dict[int, SubscriptionExecutionStatus]:
        """批量投影订阅状态，避免列表接口逐条查询。"""
        ids = tuple(dict.fromkeys(subscription_ids))
        if not ids:
            return {}
        tasks = await self._repository.latest_search_tasks(ids)
        result: dict[int, SubscriptionExecutionStatus] = {}
        for subscription_id in ids:
            task = tasks.get(subscription_id)
            if task:
                result[subscription_id] = self._from_task(task)
        return result

    async def list_batches(
        self,
        *,
        accessible_subscription_ids: Optional[set[int]],
        limit: int = 10,
    ) -> list[SubscriptionBatchStatus]:
        """列出访问范围完整覆盖的最近批次。"""
        batches = await self._repository.list_batches(limit=max(1, min(limit, 50)))
        result = []
        for batch in batches:
            tasks = await self._repository.list_batch_tasks(batch.batch_id)
            if not self._can_access_tasks(tasks, accessible_subscription_ids):
                continue
            result.append(self._from_batch(batch, tasks))
        return result

    async def get_batch(
        self,
        batch_id: str,
        *,
        accessible_subscription_ids: Optional[set[int]],
    ) -> Optional[SubscriptionBatchStatus]:
        """读取一个访问范围完整覆盖的批次。"""
        batch = await self._repository.get_batch(batch_id)
        if batch is None:
            return None
        tasks = await self._repository.list_batch_tasks(batch_id)
        if not self._can_access_tasks(tasks, accessible_subscription_ids):
            return None
        return self._from_batch(batch, tasks)

    @classmethod
    def _from_task(cls, task: SearchTaskSnapshot) -> SubscriptionExecutionStatus:
        """把搜索任务状态归一为稳定业务词汇。"""
        if task.cancel_requested and task.state == "running":
            state = phase = "cancelling"
        elif task.state == "running":
            state = phase = task.phase or "running"
        elif task.state == "queued" and task.phase == "waiting_site_budget":
            state = phase = "waiting_site_budget"
        else:
            state = phase = task.state
        return SubscriptionExecutionStatus(
            state=state,
            phase=phase,
            source=task.source,
            batch_id=task.batch_id,
            task_id=task.task_id,
            current_site_id=task.current_site_id,
            updated_at=task.updated_at,
            error=cls._safe_error(task.last_error),
            can_cancel=state in cls._ACTIVE_STATES,
        )

    @classmethod
    def _from_batch(
        cls,
        batch: SearchBatchSnapshot,
        tasks: list[SearchTaskSnapshot],
    ) -> SubscriptionBatchStatus:
        """组合批次计数与当前运行任务。"""
        current = next((task for task in tasks if task.state == "running"), None)
        if current is None:
            current = next((task for task in tasks if task.state == "queued"), None)
        processed = (
            batch.finished_count
            + batch.failed_count
            + batch.cancelled_count
            + batch.skipped_count
        )
        phase = current.phase if current else batch.state
        return SubscriptionBatchStatus(
            batch_id=batch.batch_id,
            source=batch.source,
            state=batch.state,
            phase=phase,
            total_count=batch.total_count,
            processed_count=processed,
            finished_count=batch.finished_count,
            failed_count=batch.failed_count,
            cancelled_count=batch.cancelled_count,
            skipped_count=batch.skipped_count,
            current_subscription_id=current.subscription_id if current else None,
            current_site_id=current.current_site_id if current else None,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            error=cls._safe_error(batch.last_error),
            can_cancel=batch.state in {"queued", "running", "cancelling"} and not batch.cancel_requested,
        )

    @staticmethod
    def _can_access_tasks(
        tasks: list[SearchTaskSnapshot],
        accessible_subscription_ids: Optional[set[int]],
    ) -> bool:
        """超级用户不限制；普通用户必须拥有批次内全部订阅。"""
        if accessible_subscription_ids is None:
            return True
        return bool(tasks) and all(task.subscription_id in accessible_subscription_ids for task in tasks)

    @staticmethod
    def _safe_error(error: Optional[str]) -> Optional[str]:
        """压平并限制内部错误文本，避免把堆栈或超长响应暴露给界面。"""
        if not error:
            return None
        from app.runtime.errors import public_error_message

        return public_error_message(error, context="subscription")[:500]
