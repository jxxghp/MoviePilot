"""立即执行 Agent 自主定时任务工具。"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.agenttask_oper import AgentTaskOper


class RunAgentTaskInput(BaseModel):
    """立即执行 Agent 自主定时任务的输入参数。"""

    task_id: int = Field(
        ...,
        ge=1,
        description=(
            "Integer autonomous task ID returned by query_agent_tasks. Do not pass a "
            "runtime scheduler job_id such as agent-task-12."
        ),
    )


class RunAgentTaskTool(MoviePilotTool):
    """将当前用户的 Agent 自主定时任务提交为立即执行。"""

    name: str = "run_agent_task"
    tags: list[str] = [ToolTag.Write, ToolTag.AgentTask, ToolTag.Admin]
    description: str = (
        "Queue an enabled autonomous agent task owned by the current user for immediate "
        "execution. Use the integer task_id returned by query_agent_tasks. The task runs "
        "after the current agent turn can finish and broadcasts its result through the "
        "configured notification channels."
    )
    args_schema: Type[BaseModel] = RunAgentTaskInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成立即执行 Agent 任务的提示消息。"""
        return f"立即执行自主定时任务：{kwargs.get('task_id', '')}"

    def _get_task_state(self, task_id: int) -> tuple[str, Optional[str]]:
        """校验任务归属和状态，返回可执行性及任务名称。"""
        task = AgentTaskOper().get(
            task_id=task_id,
            user_id=str(self._user_id),
        )
        if not task:
            return "not_found", None
        if not task.enabled:
            return "disabled", task.name
        if task.last_status == "running":
            return "running", task.name
        return "ready", task.name

    async def run(self, task_id: int, **kwargs: object) -> str:
        """立即执行当前用户拥有且已启用的 Agent 自主定时任务。"""
        from app.scheduler import Scheduler

        payload = RunAgentTaskInput(task_id=task_id)
        status, task_name = await self.run_blocking(
            "db",
            self._get_task_state,
            payload.task_id,
        )
        if status == "not_found":
            return f"Agent 定时任务 {task_id} 不存在或不属于当前用户"
        if status == "disabled":
            return f"Agent 定时任务 {task_id} 已暂停，请先恢复后再执行"
        if status == "running":
            return f"Agent 定时任务 {task_id} 正在执行，请勿重复触发"
        if not Scheduler().start_agent_task(payload.task_id):
            return f"Agent 定时任务 {task_id} 尚未注册到运行时调度器，无法立即执行"
        return (
            f"Agent 定时任务 {task_id} 已提交立即执行：{task_name}。"
            "执行完成后将通过已配置的通知渠道广播结果"
        )
