"""运行定时服务工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.log import logger


class RunSchedulerInput(BaseModel):
    """运行定时服务工具的输入参数模型"""

    job_id: str = Field(
        ...,
        description=(
            "Runtime scheduler job ID returned by query_schedulers. Do not pass an "
            "autonomous agent task ID or an agent-task-* runtime ID."
        ),
    )


class RunSchedulerTool(MoviePilotTool):
    """立即运行系统、插件或工作流注册的定时服务。"""

    name: str = "run_scheduler"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Scheduler,
        ToolTag.Admin,
    ]
    description: str = (
        "Manually trigger a MoviePilot system, plugin, or workflow scheduler service by "
        "the runtime job_id returned from query_schedulers. This tool does not run "
        "user-created autonomous agent tasks; use run_agent_task with an integer task_id."
    )
    args_schema: Type[BaseModel] = RunSchedulerInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """根据运行参数生成友好的提示消息。"""
        job_id = kwargs.get("job_id", "")
        return f"运行定时服务 (ID: {job_id})"

    @staticmethod
    def _run_scheduler_sync(job_id: str) -> tuple[bool, str]:
        """同步触发定时服务，避免调度器扫描阻塞事件循环。"""
        from app.scheduler import Scheduler

        scheduler = Scheduler()
        for scheduler_item in scheduler.list():
            if scheduler_item.id == job_id:
                scheduler.start(job_id)
                return True, scheduler_item.name
        return False, ""

    async def run(self, job_id: str, **kwargs: object) -> str:
        """立即运行非 Agent 自主任务的运行时定时服务。"""
        logger.info(f"执行工具: {self.name}, 参数: job_id={job_id}")

        try:
            from app.scheduler import AGENT_TASK_JOB_PREFIX

            if job_id.startswith(f"{AGENT_TASK_JOB_PREFIX}-"):
                return (
                    "Agent 自主定时任务不能通过 run_scheduler 运行，"
                    "请使用 query_agent_tasks 查询整数 task_id 后调用 run_agent_task"
                )
            job_exists, job_name = await self.run_blocking(
                "workflow", self._run_scheduler_sync, job_id
            )
            if not job_exists:
                return f"定时服务 ID {job_id} 不存在，请使用 query_schedulers 工具查询可用的定时服务"

            return f"成功触发定时服务：{job_name} (ID: {job_id})"
        except Exception as e:
            logger.error(f"运行定时服务失败: {e}", exc_info=True)
            return f"运行定时服务时发生错误: {str(e)}"
