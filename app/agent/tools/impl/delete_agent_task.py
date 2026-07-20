from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.agenttask_oper import AgentTaskOper


class DeleteAgentTaskInput(BaseModel):
    """删除 Agent 自主定时任务的输入参数。"""

    task_id: int = Field(..., ge=1, description="ID of the task to permanently delete.")


class DeleteAgentTaskTool(MoviePilotTool):
    """永久删除 Agent 自主定时任务。"""

    name: str = "delete_agent_task"
    tags: list[str] = [ToolTag.Write, ToolTag.AgentTask, ToolTag.Admin]
    description: str = (
        "Permanently delete an autonomous agent task and remove its runtime schedule. "
        "Use update_agent_task with enabled=false when the user only wants to pause it."
    )
    args_schema: Type[BaseModel] = DeleteAgentTaskInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成删除定时任务的提示消息。"""
        return f"删除自主定时任务：{kwargs.get('task_id', '')}"

    def _delete_task(self, task_id: int) -> bool:
        """删除当前用户的任务并移除运行时调度。"""
        from app.scheduler import Scheduler

        deleted = AgentTaskOper().delete(
            task_id=task_id,
            user_id=str(self._user_id),
        )
        if deleted:
            Scheduler().remove_agent_task_job(task_id)
        return deleted

    async def run(self, task_id: int, **kwargs: object) -> str:
        """删除 Agent 自主定时任务。"""
        payload = DeleteAgentTaskInput(task_id=task_id)
        deleted = await self.run_blocking("db", self._delete_task, payload.task_id)
        if not deleted:
            return f"Agent 定时任务 {task_id} 不存在或不属于当前用户"
        return f"Agent 定时任务 {task_id} 已删除"
