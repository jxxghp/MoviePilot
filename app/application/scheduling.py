"""调度器工具服务门面。

Agent 工具与 API 端点对运行时调度器的操作统一经本模块调用，
Scheduler 实现由 startup 组合根在导入期注册，避免 application 层
静态依赖顶层 scheduler 模块（scheduler 反向依赖 chain，会成环）。

依赖方向：

    agent.tools / api.endpoints -> application.scheduling <- startup（注册 Scheduler 类）
"""

from typing import Any, List, Optional

# Agent 自主定时任务在运行时调度器中的任务 ID 前缀。
AGENT_TASK_JOB_PREFIX = "agent-task"

# Scheduler 类：由 startup/scheduler_initializer 在导入期注册。
_scheduler_class: Any = None


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


def list_scheduler_jobs() -> List[Any]:
    """列出运行时调度器的全部任务。"""
    return get_scheduler().list()


def start_scheduler_job(job_id: str) -> None:
    """立即运行指定的运行时定时任务。"""
    get_scheduler().start(job_id)


def update_plugin_job(plugin_id: str) -> None:
    """更新插件的定时任务。"""
    get_scheduler().update_plugin_job(plugin_id)


def remove_plugin_job(plugin_id: str) -> None:
    """移除插件的定时任务。"""
    get_scheduler().remove_plugin_job(plugin_id)


def start_agent_task(task_id: int) -> bool:
    """立即执行 Agent 自主定时任务。"""
    return get_scheduler().start_agent_task(task_id)


def get_agent_task_next_run(task_id: int) -> Optional[Any]:
    """查询 Agent 自主定时任务的下一次运行时间。"""
    return get_scheduler().get_agent_task_next_run(task_id)


def update_agent_task_job(task_id: int) -> Optional[Any]:
    """更新 Agent 自主定时任务的注册信息，返回下一次运行时间。"""
    return get_scheduler().update_agent_task_job(task_id)


def remove_agent_task_job(task_id: int) -> None:
    """移除 Agent 自主定时任务的注册信息。"""
    get_scheduler().remove_agent_task_job(task_id)
