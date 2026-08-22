"""调度器工具服务门面。

Agent 工具与 API 端点对运行时调度器的操作统一经本模块调用，
Scheduler 实现由 startup 组合根在导入期注册，避免 application 层
静态依赖顶层 scheduler 模块（scheduler 反向依赖 chain，会成环）。

依赖方向：

    agent.tools / api.endpoints -> application.scheduling <- startup（注册 Scheduler 类）
"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, List, Optional, cast

# Agent 自主定时任务在运行时调度器中的任务 ID 前缀。
AGENT_TASK_JOB_PREFIX = "agent-task"

# Scheduler 类：由 startup/scheduler_initializer 在导入期注册。
_scheduler_class: Any = None


class JobOverlapPolicy(StrEnum):
    """描述同一 job 已运行时的新触发处理策略。"""

    SKIP = "skip"


class JobRecoveryPolicy(StrEnum):
    """描述进程重启后是否以及如何重建执行意图。"""

    NEXT_SCHEDULE = "next_schedule"
    DURABLE_QUEUE = "durable_queue"
    MANUAL_ONLY = "manual_only"


@dataclass(frozen=True, slots=True)
class JobSpec:
    """数据化声明一个业务 job 的执行和恢复合同。"""

    job_id: str
    name: str
    func: Callable[..., Any]
    owner: str
    overlap: JobOverlapPolicy = JobOverlapPolicy.SKIP
    timeout_seconds: int | None = None
    manual: bool = False
    recovery: JobRecoveryPolicy = JobRecoveryPolicy.NEXT_SCHEDULE
    kwargs: dict[str, Any] = field(default_factory=dict)

    def to_runtime_state(self) -> dict[str, Any]:
        """生成兼容 Scheduler Facade 的可变执行状态。"""
        return {
            "name": self.name,
            "func": self.func,
            "owner": self.owner,
            "overlap": self.overlap.value,
            "timeout_seconds": self.timeout_seconds,
            "manual": self.manual,
            "recovery": self.recovery.value,
            "kwargs": dict(self.kwargs),
            "running": False,
        }


class JobCatalog:
    """保存唯一 job ID 到声明的映射。"""

    def __init__(self, specs: list[JobSpec]) -> None:
        """拒绝重复 ID，并冻结供 Scheduler 初始化的声明集合。"""
        self._specs = {spec.job_id: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("JobSpec job_id 不得重复")

    def runtime_states(self) -> dict[str, dict[str, Any]]:
        """返回兼容旧 Scheduler `_jobs` 的全新状态字典。"""
        return {
            job_id: spec.to_runtime_state()
            for job_id, spec in self._specs.items()
        }


class JobExecutionState:
    """集中维护兼容 job 字典的 overlap 与终态字段。"""

    @staticmethod
    def begin(job: dict[str, Any], started_at: str) -> bool:
        """按 overlap policy 尝试进入 running，已运行时返回 False。"""
        if job.get("running") and job.get(
            "overlap", JobOverlapPolicy.SKIP.value
        ) == JobOverlapPolicy.SKIP.value:
            return False
        job.update(
            running=True,
            last_started_at=started_at,
            last_finished_at=None,
            last_error=None,
        )
        return True

    @staticmethod
    def finish(job: dict[str, Any], finished_at: str, error: str | None) -> None:
        """写入成功或失败终态并释放 running 标记。"""
        job.update(
            running=False,
            last_finished_at=finished_at,
            last_error=error,
        )

    @staticmethod
    async def await_result(
        awaitable: Awaitable[Any], timeout_seconds: int | None
    ) -> Any:
        """等待协程任务；声明了超时时由 asyncio 负责取消底层任务。"""
        if timeout_seconds is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def register_scheduler_class(scheduler_class: Any) -> None:
    """注册 Scheduler 类（组合根在导入期调用）。"""
    global _scheduler_class
    _scheduler_class = scheduler_class


def get_scheduler() -> Any:
    """返回调度器实例。"""
    if _scheduler_class is None:
        raise RuntimeError(
            "调度器服务未初始化：请先通过 register_scheduler_class 注册 Scheduler 类"
        )
    return _scheduler_class()


class Scheduler:
    """应用层调度器兼容门面，不直接导入顶层 Scheduler 实现。"""

    def __new__(cls) -> Any:
        """返回组合根注册的调度器实例。"""
        return get_scheduler()


def list_scheduler_jobs() -> List[Any]:
    """列出运行时调度器的全部任务。"""
    return cast(List[Any], get_scheduler().list())


def start_scheduler_job(job_id: str, **kwargs: Any) -> None:
    """立即运行指定的运行时定时任务。"""
    get_scheduler().start(job_id, **kwargs)


def update_plugin_job(plugin_id: str) -> None:
    """更新插件的定时任务。"""
    get_scheduler().update_plugin_job(plugin_id)


def remove_plugin_job(plugin_id: str) -> None:
    """移除插件的定时任务。"""
    get_scheduler().remove_plugin_job(plugin_id)


def start_agent_task(task_id: int) -> bool:
    """立即执行 Agent 自主定时任务。"""
    return cast(bool, get_scheduler().start_agent_task(task_id))


def get_agent_task_next_run(task_id: int) -> Optional[Any]:
    """查询 Agent 自主定时任务的下一次运行时间。"""
    return get_scheduler().get_agent_task_next_run(task_id)


def update_agent_task_job(task_id: int) -> Optional[Any]:
    """更新 Agent 自主定时任务的注册信息，返回下一次运行时间。"""
    return get_scheduler().update_agent_task_job(task_id)


def remove_agent_task_job(task_id: int) -> None:
    """移除 Agent 自主定时任务的注册信息。"""
    get_scheduler().remove_agent_task_job(task_id)
