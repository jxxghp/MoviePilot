"""Agent 自主定时任务执行的异步应用边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import uuid4

from app.application.database import AsyncDatabaseExecutor
from app.runtime.execution import await_task_to_terminal
from app.schemas.exception import DatabaseWorkerOverloadedError


T = TypeVar("T")
SyncTransaction = Callable[[Callable[[object], T]], T]


class AgentTaskRecord(Protocol):
    """执行认领与终态判定所需的任务投影。"""

    id: int
    enabled: bool
    last_run_id: str | None
    last_status: str


class AgentTaskRunRecord(Protocol):
    """执行期间需要脱离数据库会话持有的运行记录字段。"""

    run_id: str
    task_id: int
    trigger_source: str
    name: str
    content: str
    trigger_type: str
    cron_expression: str | None
    run_at: str | None
    user_id: str
    username: str | None
    session_id: str


class AgentTaskRepository(Protocol):
    """AgentTask 执行用例使用的同步短事务仓储合同。"""

    def get(self, task_id: int) -> AgentTaskRecord | None:
        """读取任务当前投影。"""

    def begin_run(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        run_id: str | None = None,
    ) -> AgentTaskRunRecord | None:
        """原子认领任务并创建运行快照。"""

    def finish_run_outcome(
        self,
        run_id: str,
        success: bool,
        result: str,
    ) -> AgentTaskFinishRecord:
        """收口运行并返回事务确认的终态事实。"""


class AgentTaskFinishRecord(Protocol):
    """同步仓储返回的结构化运行终态。"""

    run_finalized: bool
    task_projection_updated: bool
    date_task_disabled: bool


AgentTaskRepositoryFactory = Callable[[object], AgentTaskRepository]
AgentTaskScheduleRemover = Callable[[int, int, str], bool]


@dataclass(frozen=True, slots=True)
class AgentTaskRunSnapshot:
    """任务认领成功后可安全跨越数据库会话的执行快照。"""

    run_id: str
    task_id: int
    trigger_source: str
    name: str
    content: str
    trigger_type: str
    cron_expression: str | None
    run_at: str | None
    user_id: str
    username: str | None
    session_id: str


@dataclass(frozen=True, slots=True)
class AgentTaskClaim:
    """任务认领结果；拒绝原因保持现有 Agent 用户提示合同。"""

    run: AgentTaskRunSnapshot | None
    rejection: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTaskFinishOutcome:
    """区分运行收口、任务投影更新与一次任务停用三个事实。"""

    run_finalized: bool
    task_projection_updated: bool
    date_task_disabled: bool


class AgentTaskExecutionService:
    """通过有界数据库 worker 认领并收口一次 AgentTask 执行。"""

    def __init__(
        self,
        *,
        repository: AgentTaskRepositoryFactory,
        async_executor: AsyncDatabaseExecutor,
        sync_transaction: SyncTransaction,
    ) -> None:
        """保存同步仓储、事务和异步执行器。"""
        self._repository = repository
        self._async_executor = async_executor
        self._sync_transaction = sync_transaction

    @staticmethod
    def _snapshot(run: AgentTaskRunRecord) -> AgentTaskRunSnapshot:
        """在事务内复制运行字段，避免 ORM 对象越过会话边界。"""
        return AgentTaskRunSnapshot(
            run_id=run.run_id,
            task_id=run.task_id,
            trigger_source=run.trigger_source,
            name=run.name,
            content=run.content,
            trigger_type=run.trigger_type,
            cron_expression=run.cron_expression,
            run_at=run.run_at,
            user_id=run.user_id,
            username=run.username,
            session_id=run.session_id,
        )

    async def claim(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        scheduler_generation: int | None = None,
        remove_schedule: AgentTaskScheduleRemover | None = None,
    ) -> AgentTaskClaim:
        """认领一次执行；取消发生在提交后时先补偿收口再传播取消。"""

        run_id = uuid4().hex

        def transaction(session: object) -> AgentTaskClaim:
            repository = self._repository(session)
            run = repository.begin_run(
                task_id=task_id,
                trigger_source=trigger_source,
                run_id=run_id,
            )
            if not run:
                task = repository.get(task_id)
                return AgentTaskClaim(
                    run=None,
                    rejection=(
                        "Agent 定时任务不存在或已停用"
                        if not task or not task.enabled
                        else "Agent 定时任务当前不可执行"
                    ),
                )
            return AgentTaskClaim(run=self._snapshot(run))

        async def claim_to_terminal() -> AgentTaskClaim:
            """容量瞬时耗尽时保留本轮调度，直到认领取得 admission。"""
            while True:
                try:
                    return await self._async_executor.run(
                        lambda: self._sync_transaction(transaction)
                    )
                except DatabaseWorkerOverloadedError:
                    await asyncio.sleep(0.01)

        claim_task = asyncio.create_task(claim_to_terminal())
        try:
            return await claim_task
        except asyncio.CancelledError as cancellation:
            finalize_task = asyncio.create_task(self._finalize(
                run_id=run_id,
                task_id=task_id,
                success=False,
                result="Agent 定时任务已取消",
                scheduler_generation=scheduler_generation,
                remove_schedule=remove_schedule,
            ))
            await await_task_to_terminal(finalize_task)
            raise cancellation

    async def finalize(
        self,
        run: AgentTaskRunSnapshot,
        *,
        success: bool,
        result: str,
        scheduler_generation: int | None = None,
        remove_schedule: AgentTaskScheduleRemover | None = None,
    ) -> AgentTaskFinishOutcome:
        """等待终态事务完成，并仅清理仍属于该 generation 的一次任务。"""

        return await self._finalize(
            run_id=run.run_id,
            task_id=run.task_id,
            success=success,
            result=result,
            scheduler_generation=scheduler_generation,
            remove_schedule=remove_schedule,
        )

    async def _finalize(
        self,
        *,
        run_id: str,
        task_id: int,
        success: bool,
        result: str,
        scheduler_generation: int | None,
        remove_schedule: AgentTaskScheduleRemover | None,
    ) -> AgentTaskFinishOutcome:
        """按稳定运行 ID 收口，供正常路径和取消补偿共享。"""

        def transaction(session: object) -> AgentTaskFinishOutcome:
            repository = self._repository(session)
            outcome = repository.finish_run_outcome(
                run_id=run_id,
                success=success,
                result=result,
            )
            return AgentTaskFinishOutcome(
                run_finalized=outcome.run_finalized,
                task_projection_updated=outcome.task_projection_updated,
                date_task_disabled=outcome.date_task_disabled,
            )

        async def finish_to_terminal() -> AgentTaskFinishOutcome:
            """容量瞬时耗尽时保留 owner，直到收口取得 admission。"""
            while True:
                try:
                    return await self._async_executor.run(
                        lambda: self._sync_transaction(transaction)
                    )
                except DatabaseWorkerOverloadedError:
                    await asyncio.sleep(0.01)

        finish_task = asyncio.create_task(finish_to_terminal())
        cancellation: asyncio.CancelledError | None = None
        try:
            outcome = await asyncio.shield(finish_task)
        except asyncio.CancelledError as error:
            cancellation = error
            outcome = await await_task_to_terminal(finish_task)

        if (
            outcome.date_task_disabled
            and scheduler_generation is not None
            and remove_schedule is not None
        ):
            remove_schedule(task_id, scheduler_generation, run_id)
        if cancellation is not None:
            raise cancellation
        return outcome


_service: AgentTaskExecutionService | None = None


def configure_agent_task_execution(service: AgentTaskExecutionService) -> None:
    """由启动组合根登记 AgentTask 执行服务。"""
    global _service
    _service = service


def get_agent_task_execution_service() -> AgentTaskExecutionService:
    """返回已登记的 AgentTask 执行服务。"""
    if _service is None:
        raise RuntimeError("AgentTask 执行服务尚未配置")
    return _service
