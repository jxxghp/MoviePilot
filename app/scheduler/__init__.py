"""定时服务组合根。

调度器把通用执行引擎与 Agent、工作流、插件三类领域作业的登记路径装配到一起，
宿主业务作业清单由 ``app.startup.bindings.scheduling`` 提供。
"""

from app.application.scheduling import AGENT_TASK_JOB_PREFIX
from app.runtime.scheduler import SCHEDULER_PROGRESS_PREFIX, lock
from app.scheduler.agent_tasks import AgentTaskScheduling
from app.scheduler.composition import Scheduler
from app.scheduler.plugins import PluginScheduling
from app.scheduler.workflows import WorkflowScheduling

__all__ = [
    "AGENT_TASK_JOB_PREFIX",
    "AgentTaskScheduling",
    "PluginScheduling",
    "SCHEDULER_PROGRESS_PREFIX",
    "Scheduler",
    "WorkflowScheduling",
    "lock",
]
