"""
订阅执行状态的业务投影与访问范围治理。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional, Protocol

from app.application.subscription.execution import SearchBatchSnapshot, SearchTaskSnapshot
from app.schemas.common import JsonData


@dataclass(frozen=True, slots=True)
class SubscriptionExecutionStatus:
    """
    一个订阅最近搜索任务的用户可见状态。

    :param state: 当前任务或批次的业务状态
    :param phase: 当前执行阶段，用于展示进度与恢复位置
    :param updated_at: 最近状态更新时间文本
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param batch_id: 持久搜索批次的稳定标识
    :param task_id: 持久搜索任务的稳定标识；无队列任务时为 None
    :param current_site_id: 正在处理的站点标识；尚未进入站点时为 None
    :param next_run_at: 下一次允许执行的时间文本；没有计划时为 None
    :param error: 可展示的错误摘要；无错误时为 None
    :param can_cancel: 当前状态是否支持用户取消
    """

    state: str
    phase: str
    updated_at: str
    source: Optional[str] = None
    batch_id: Optional[str] = None
    task_id: Optional[str] = None
    current_site_id: Optional[int] = None
    next_run_at: Optional[str] = None
    error: Optional[str] = None
    can_cancel: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionBatchStatus:
    """
    订阅搜索批次的进度、当前工作和操作能力。

    :param batch_id: 持久搜索批次的稳定标识
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param state: 当前任务或批次的业务状态
    :param phase: 当前执行阶段，用于展示进度与恢复位置
    :param total_count: 批次包含的任务总数
    :param processed_count: 批次已结束的任务数，含完成、失败、取消和跳过
    :param finished_count: 成功完成的任务数量
    :param failed_count: 失败终止的任务数量
    :param cancelled_count: 已取消的任务数量
    :param created_at: 记录创建时间文本
    :param updated_at: 最近状态更新时间文本
    :param current_subscription_id: 批次当前正在处理的订阅主键；未运行时为 None
    :param current_site_id: 正在处理的站点标识；尚未进入站点时为 None
    :param error: 可展示的错误摘要；无错误时为 None
    :param can_cancel: 当前状态是否支持用户取消
    :param download_counts: 成功下载任务的分类计数，与搜索任务成功/失败计数独立
    :param downloads: 批次内成功下载安全摘要；最多返回 100 条
    :param downloads_omitted: 超出明细上限或已被活动裁剪的成功下载数量
    :param skipped_count: 本轮无需执行而跳过的任务数量
    """

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
    download_counts: dict[str, int] = field(default_factory=dict)
    downloads: tuple[dict[str, JsonData], ...] = ()
    downloads_omitted: int = 0


class SubscriptionExecutionReadRepository(Protocol):
    """
    请求级读取搜索任务与批次事实的端口。
    """

    async def latest_search_tasks(
        self,
        subscription_ids: tuple[int, ...],
    ) -> dict[int, SearchTaskSnapshot]:
        """
        返回每条订阅最近更新的搜索任务。

        :param subscription_ids: 目标订阅主键，保持调用方指定的顺序
        :return: 以订阅主键为键的最近任务快照；没有任务的订阅不写入映射
        """
        ...

    async def list_batches(
        self,
        *,
        limit: int,
    ) -> list[SearchBatchSnapshot]:
        """
        返回最近更新的搜索批次。

        :param limit: 最多返回的记录数量
        :return: 按最近更新时间排列且不超过上限的批次快照
        """
        ...

    async def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """
        按稳定 ID 返回一个搜索批次。

        :param batch_id: 持久搜索批次的稳定标识
        :return: 对应批次的最新快照；不存在时返回 None
        """
        ...

    async def list_batch_tasks(self, batch_id: str) -> list[SearchTaskSnapshot]:
        """
        按稳定位置返回批次任务。

        :param batch_id: 持久搜索批次的稳定标识
        :return: 按批次内稳定位置排列的任务快照列表
        """
        ...


class SubscriptionExecutionStatusService:
    """
    把搜索队列投影为稳定业务状态。
    """

    _ACTIVE_STATES = {
        "queued",
        "running",
        "matching",
        "searching",
        "scheduled",
        "waiting_subscription",
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
        """
        保存请求会话绑定的状态读取端口和可选取消用例。
        """
        self._repository = repository
        self._request_cancel = request_cancel

    async def request_cancel(self, batch_id: str) -> bool:
        """
        通过组合根提供的执行边界请求取消搜索批次。
        """
        if self._request_cancel is None:
            raise RuntimeError("订阅搜索批次取消能力未注册")
        return bool(await self._request_cancel(batch_id))

    async def for_subscriptions(
        self,
        subscription_ids: tuple[int, ...],
    ) -> dict[int, SubscriptionExecutionStatus]:
        """
        批量投影订阅状态，避免列表接口逐条查询。
        """
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
        """
        列出访问范围完整覆盖的最近批次。
        """
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
        """
        读取一个访问范围完整覆盖的批次。
        """
        batch = await self._repository.get_batch(batch_id)
        if batch is None:
            return None
        tasks = await self._repository.list_batch_tasks(batch_id)
        if not self._can_access_tasks(tasks, accessible_subscription_ids):
            return None
        return self._from_batch(batch, tasks)

    @classmethod
    def _from_task(cls, task: SearchTaskSnapshot) -> SubscriptionExecutionStatus:
        """
        把搜索任务状态归一为稳定业务词汇。
        """
        if task.cancel_requested and task.state == "running":
            state = phase = "cancelling"
        elif task.state == "running":
            state = phase = task.phase or "running"
        elif task.state == "queued" and task.phase in cls._ACTIVE_STATES:
            state = phase = task.phase
        else:
            state = phase = task.state
        return SubscriptionExecutionStatus(
            state=state,
            phase=phase,
            source=task.source,
            batch_id=task.batch_id,
            task_id=task.task_id,
            current_site_id=task.current_site_id,
            next_run_at=task.available_at if task.state == "queued" else None,
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
        """
        组合批次计数与当前运行任务。
        """
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
        download_counts = {key: sum(task.download_counts.get(key, 0) for task in tasks)
                           for key in ("submitted", "initial_submitted", "upgrade_submitted", "full_upgrade_submitted")}
        downloads = tuple(resource for task in tasks for resource in task.downloads)[:100]
        return SubscriptionBatchStatus(
            download_counts=download_counts,
            downloads=downloads,
            downloads_omitted=max(0, download_counts["submitted"] - len(downloads)),
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
        """
        超级用户不限制；普通用户必须拥有批次内全部订阅。
        """
        if accessible_subscription_ids is None:
            return True
        return bool(tasks) and all(task.subscription_id in accessible_subscription_ids for task in tasks)

    @staticmethod
    def _safe_error(error: Optional[str]) -> Optional[str]:
        """
        压平并限制内部错误文本，避免把堆栈或超长响应暴露给界面。
        """
        if not error:
            return None
        from app.runtime.errors import public_error_message

        return public_error_message(error, context="subscription")[:500]
