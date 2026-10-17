"""
Agent 自主定时任务执行的异步应用边界。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import List, Optional, Protocol, TypeVar
from uuid import uuid4

from app.application.database import AsyncDatabaseExecutor
from app.runtime.execution import await_task_to_terminal
from app.schemas.exception import DatabaseWorkerOverloadedError

T = TypeVar("T")


class SyncTransaction(Protocol):
    """
    在同步事务内执行回调，每次调用独立保留回调的结果类型。
    """

    def __call__(self, operation: Callable[[object], T], /) -> T:
        """
        由事务 owner 提供会话，并原样返回本次操作结果。

        :param operation: 接收当前事务会话的同步业务操作；正常返回后提交，异常时回滚
        :return: 操作返回的原始结果，仅在事务提交完成后返回
        """
        ...


@dataclass(frozen=True, slots=True)
class AgentTaskSnapshot:
    """
    脱离数据库 Session 的自主任务完整快照。

    :param id: 持久化任务主键
    :param name: 任务展示名称
    :param content: 触发执行时提交给 Agent 的任务指令
    :param trigger_type: 触发类型：cron 为周期任务，date 为一次性任务
    :param cron_expression: 周期任务的 Cron 表达式，非周期任务可为 None
    :param run_at: 一次性任务的计划执行时间字符串，周期任务可为 None
    :param enabled: 任务是否允许调度执行
    :param user_id: 任务所属真实用户标识
    :param username: 用户显示名称，未提供时为 None
    :param session_id: 任务绑定的 Agent 会话标识
    :param channel: 结果通知使用的渠道，未配置时为 None
    :param source: 通知渠道实例标识，未配置时为 None
    :param original_chat_id: 结果回复使用的渠道聊天标识，未配置时为 None
    :param last_status: 任务最近一次运行的状态投影
    :param last_run_at: 最近一次开始运行的时间字符串，未运行时为 None
    :param last_result: 最近一次运行的结果摘要，未产生时为 None
    :param last_run_id: 最近一次运行标识，未运行时为 None
    :param run_count: 任务累计运行次数
    :param created_at: 任务创建时间字符串
    :param updated_at: 任务最近更新时间字符串
    """

    id: int
    name: str
    content: str
    trigger_type: str
    cron_expression: str | None
    run_at: str | None
    enabled: bool
    user_id: str
    username: str | None
    session_id: str
    channel: str | None
    source: str | None
    original_chat_id: str | None
    last_status: str
    last_run_at: str | None
    last_result: str | None
    last_run_id: str | None
    run_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentTaskRunSnapshot:
    """
    脱离数据库 Session 的自主任务运行快照。

    :param run_id: 本次运行的稳定标识，用于收口与去重
    :param task_id: 所属持久化任务主键
    :param trigger_source: 运行触发来源，区分定时调度与用户手动触发
    :param name: 任务展示名称
    :param content: 触发执行时提交给 Agent 的任务指令
    :param trigger_type: 触发类型：cron 为周期任务，date 为一次性任务
    :param cron_expression: 周期任务的 Cron 表达式，非周期任务可为 None
    :param run_at: 一次性任务的计划执行时间字符串，周期任务可为 None
    :param user_id: 任务所属真实用户标识
    :param username: 用户显示名称，未提供时为 None
    :param session_id: 任务绑定的 Agent 会话标识
    :param channel: 结果通知使用的渠道，未配置时为 None
    :param message_source: 本次运行快照中的通知渠道实例标识
    :param original_chat_id: 结果回复使用的渠道聊天标识，未配置时为 None
    :param status: 本次运行的持久化执行状态
    :param started_at: 本次运行开始时间字符串
    :param finished_at: 本次运行结束时间字符串，尚未结束时为 None
    :param result: 本次运行的结果摘要，尚未产生时为 None
    """

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
    channel: str | None
    message_source: str | None
    original_chat_id: str | None
    status: str
    started_at: str
    finished_at: str | None
    result: str | None


class AgentTaskRepository(Protocol):
    """
    调度、工具与执行用例共享的类型化自主任务仓储合同。
    """

    def add(self, **values: object) -> AgentTaskSnapshot | None:
        """
        新增任务并返回提交后的冻结快照。

        :param values: 包含名称、任务指令、触发配置及用户会话归属的创建字段
        :return: 提交成功后的任务快照，无法创建时为 None
        """
        ...

    def get(
        self,
        task_id: int,
        user_id: Optional[str] = None,
    ) -> AgentTaskSnapshot | None:
        """
        读取任务当前投影。

        :param task_id: 持久化任务主键
        :param user_id: 限定任务归属的用户标识，None 表示不按用户过滤
        :return: 匹配的任务快照，不存在或不属于指定用户时为 None
        """
        ...

    def list(
        self,
        user_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> list[AgentTaskSnapshot]:
        """
        按归属和启用状态读取任务快照。

        :param user_id: 限定任务归属的用户标识，None 表示不按用户过滤
        :param enabled: 启用状态筛选，None 表示不限制
        :return: 符合归属及启用条件的任务快照列表
        """
        ...

    def update(
        self,
        task_id: int,
        payload: dict[str, object],
        user_id: Optional[str] = None,
    ) -> bool:
        """
        更新非运行中任务。

        :param task_id: 待更新的任务主键
        :param payload: 允许更新的任务定义与状态字段，其他字段由仓储忽略
        :param user_id: 限定任务归属的用户标识，None 表示不按用户过滤
        :return: 更新成功时为 True，无有效更新字段、记录不匹配或任务运行中时为 False
        """
        ...

    def delete(self, task_id: int, user_id: Optional[str] = None) -> bool:
        """
        删除非运行中任务及其运行记录。

        :param task_id: 待删除的任务主键
        :param user_id: 限定任务归属的用户标识，None 表示不按用户过滤
        :return: 删除成功时为 True，不存在、不匹配或任务正在运行时为 False
        """
        ...

    def mark_interrupted(self, task_id: int, result: str) -> bool:
        """
        把遗留运行态收口为结果未知。

        :param task_id: 需要收口遗留运行态的任务主键
        :param result: 向用户说明中断且结果未知的摘要
        :return: 遗留运行态已收口时为 True，没有可收口记录时为 False
        """
        ...

    def begin_run(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        run_id: str | None = None,
    ) -> AgentTaskRunSnapshot | None:
        """
        原子认领任务并创建运行快照。

        :param task_id: 尝试认领的任务主键
        :param trigger_source: 触发来源，默认 scheduled 表示定时调度，manual 表示手动触发
        :param run_id: 调用方指定的稳定运行标识，None 时由仓储生成
        :return: 认领成功的运行快照；无法认领时为 None，不得据此启动执行
        """
        ...

    def list_runs(
        self,
        task_id: int,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[AgentTaskRunSnapshot]:
        """
        读取指定任务最近的运行快照。

        :param task_id: 所属任务主键
        :param user_id: 限定任务归属的用户标识，None 表示不按用户过滤
        :param limit: 最多返回的最近运行条数，默认 10
        :return: 按最近运行优先排列的运行快照，任务不匹配时为空列表
        """
        ...

    def finish_run_outcome(
        self,
        run_id: str,
        success: bool,
        result: str,
    ) -> AgentTaskFinishRecord:
        """
        收口运行并返回事务确认的终态事实。

        :param run_id: 需要收口的运行标识
        :param success: 本次执行是否成功，False 将记录为失败
        :param result: 本次执行结果摘要
        :return: 已提交的运行收口、任务投影更新及一次性任务停用事实
        """


class AgentTaskFinishRecord(Protocol):
    """
    同步仓储返回的结构化运行终态。
    """

    @property
    def run_finalized(self) -> bool:
        """
        运行记录是否已进入终态。

        :return: 本次运行记录已进入终态时为 True
        """
        ...

    @property
    def task_projection_updated(self) -> bool:
        """
        任务最新运行投影是否由本次执行更新。

        :return: 任务最近运行状态确由本次运行更新时为 True
        """
        ...

    @property
    def date_task_disabled(self) -> bool:
        """
        一次性任务是否已随本次执行停用。

        :return: 本次一次性运行完成后任务已停用时为 True
        """
        ...


AgentTaskRepositoryFactory = Callable[[object], AgentTaskRepository]
AgentTaskScheduleRemover = Callable[[int, int, str], bool]


@dataclass(frozen=True, slots=True)
class AgentTaskClaim:
    """
    任务认领结果；拒绝原因保持现有 Agent 用户提示合同。

    :param run: 认领成功后冻结的运行快照，拒绝认领时为 None
    :param rejection: 可向用户展示的拒绝原因，认领成功时为 None
    """

    run: AgentTaskRunSnapshot | None
    rejection: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTaskFinishOutcome:
    """
    区分运行收口、任务投影更新与一次任务停用三个事实。

    :param run_finalized: 本次运行记录是否已成功进入终态
    :param task_projection_updated: 任务的最近运行投影是否由本次运行更新
    :param date_task_disabled: 一次性任务是否因本次运行已完成而停用
    """

    run_finalized: bool
    task_projection_updated: bool
    date_task_disabled: bool


def agent_task_to_dict(
    task: AgentTaskSnapshot,
    *,
    next_run_at: str | None = None,
    timezone: str | None = None,
) -> dict[str, object]:
    """
    把自主任务快照转换为 Agent 工具稳定返回结构。
    """
    return {
        "id": task.id,
        "name": task.name,
        "content": task.content,
        "trigger_type": task.trigger_type,
        "cron_expression": task.cron_expression,
        "run_at": task.run_at,
        "timezone": timezone,
        "enabled": task.enabled,
        "last_status": task.last_status,
        "last_run_at": task.last_run_at,
        "last_result": task.last_result,
        "last_run_id": task.last_run_id,
        "run_count": task.run_count,
        "next_run_at": next_run_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def agent_task_run_to_dict(run: AgentTaskRunSnapshot) -> dict[str, object]:
    """
    把自主任务运行快照转换为 Agent 工具稳定返回结构。
    """
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "trigger_source": run.trigger_source,
        "name": run.name,
        "content": run.content,
        "trigger_type": run.trigger_type,
        "cron_expression": run.cron_expression,
        "run_at": run.run_at,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "result": run.result,
    }


class AgentTaskExecutionService:
    """
    通过有界数据库 worker 认领并收口一次 AgentTask 执行。
    """

    def __init__(
        self,
        *,
        repository: AgentTaskRepositoryFactory,
        async_executor: AsyncDatabaseExecutor,
        sync_transaction: SyncTransaction,
    ) -> None:
        """
        保存同步仓储、事务和异步执行器。
        """
        self._repository = repository
        self._async_executor = async_executor
        self._sync_transaction = sync_transaction

    @staticmethod
    def _snapshot(run: AgentTaskRunSnapshot) -> AgentTaskRunSnapshot:
        """
        在事务内复制运行字段，避免 ORM 对象越过会话边界。
        """
        return run

    async def claim(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        *,
        scheduler_generation: int | None = None,
        remove_schedule: AgentTaskScheduleRemover | None = None,
    ) -> AgentTaskClaim:
        """
        认领一次执行；取消发生在提交后时先补偿收口再传播取消。
        """

        run_id = uuid4().hex
        run_created = threading.Event()

        def transaction(session: object) -> AgentTaskClaim:
            """
            在同一事务内认领任务或生成拒绝快照，避免泄漏 ORM 对象。
            """
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
            run_created.set()
            return AgentTaskClaim(run=self._snapshot(run))

        async def claim_to_terminal() -> AgentTaskClaim:
            """
            容量瞬时耗尽时保留本轮调度，直到认领取得 admission。
            """
            while True:
                try:
                    claim_result: AgentTaskClaim = await self._async_executor.run(
                        lambda: self._sync_transaction(transaction)
                    )
                    return claim_result
                except DatabaseWorkerOverloadedError:
                    await asyncio.sleep(0.01)

        claim_task = asyncio.create_task(claim_to_terminal())
        try:
            return await claim_task
        except asyncio.CancelledError as cancellation:
            # 纯容量拒绝尚未进入事务，不存在需要等待数据库容量的补偿对象。
            if not run_created.is_set():
                raise cancellation
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
        """
        等待终态事务完成，并仅清理仍属于该 generation 的一次任务。
        """

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
        """
        按稳定运行 ID 收口，供正常路径和取消补偿共享。
        """

        def transaction(session: object) -> AgentTaskFinishOutcome:
            """
            在同一事务内收口运行，并复制真实更新结果供调度器处理。
            """
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
            """
            容量瞬时耗尽时保留 owner，直到收口取得 admission。
            """
            while True:
                try:
                    finish_result: AgentTaskFinishOutcome = await self._async_executor.run(
                        lambda: self._sync_transaction(transaction)
                    )
                    return finish_result
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
    """
    由启动组合根登记 AgentTask 执行服务。
    """
    global _service
    _service = service


def reset_agent_task_execution() -> None:
    """
    清除当前 lifespan 的 AgentTask 执行服务。
    """
    global _service
    _service = None


def get_agent_task_execution_service() -> AgentTaskExecutionService:
    """
    返回已登记的 AgentTask 执行服务。
    """
    if _service is None:
        raise RuntimeError("AgentTask 执行服务尚未配置")
    return _service
