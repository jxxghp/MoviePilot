"""插件可使用的窄调度服务门面。"""

from app.application.scheduling import (
    add_plugin_once_job,
    get_agent_task_next_run,
    list_scheduler_jobs,
    remove_agent_task_job,
    remove_plugin_job,
    remove_plugin_once_job,
    start_agent_task,
    start_scheduler_job,
    update_agent_task_job,
    update_plugin_job,
)
from app.schemas.dashboard import ScheduleInfo, ScheduleProgress

__all__ = [
    "ScheduleInfo",
    "ScheduleProgress",
    "add_plugin_once_job",
    "get_agent_task_next_run",
    "list_scheduler_jobs",
    "remove_agent_task_job",
    "remove_plugin_job",
    "remove_plugin_once_job",
    "start_agent_task",
    "start_scheduler_job",
    "update_agent_task_job",
    "update_plugin_job",
]
