import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.config import settings
from app.db.agenttask_oper import AgentTaskOper


class QueryAgentTasksInput(BaseModel):
    """查询 Agent 自主定时任务的输入参数。"""

    task_id: Optional[int] = Field(
        None,
        ge=1,
        description="Optional task ID. Omit it to list tasks owned by the current user.",
    )
    enabled: Optional[bool] = Field(
        None,
        description="Optional enabled-state filter used when listing tasks.",
    )


class QueryAgentTasksTool(MoviePilotTool):
    """查询当前用户创建的 Agent 自主定时任务。"""

    name: str = "query_agent_tasks"
    tags: list[str] = [ToolTag.Read, ToolTag.AgentTask, ToolTag.Admin]
    description: str = (
        "Query persistent autonomous agent tasks owned by the current user, including "
        "reminders, monitoring tasks, and recurring agent work. Returns the integer "
        "task_id, instructions, trigger, enabled state, next run time, and latest result. "
        "Do not use this for MoviePilot system, plugin, or workflow scheduler services."
    )
    args_schema: Type[BaseModel] = QueryAgentTasksInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成查询定时任务的提示消息。"""
        task_id = kwargs.get("task_id")
        return f"查询自主定时任务：{task_id}" if task_id else "查询自主定时任务"

    def _query_tasks(
            self,
            task_id: Optional[int],
            enabled: Optional[bool],
    ) -> list[dict]:
        """读取当前用户的任务及运行时下一次触发时间。"""
        from app.scheduler import Scheduler

        oper = AgentTaskOper()
        if task_id:
            task = oper.get(task_id=task_id, user_id=str(self._user_id))
            tasks = [task] if task else []
        else:
            tasks = oper.list(user_id=str(self._user_id), enabled=enabled)
        scheduler = Scheduler()
        result = []
        for task in tasks:
            data = oper.to_dict(
                task,
                next_run_at=scheduler.get_agent_task_next_run(task.id),
                timezone=settings.TZ,
            )
            result.append(data)
        return result

    async def run(
            self,
            task_id: Optional[int] = None,
            enabled: Optional[bool] = None,
            **kwargs: object,
    ) -> str:
        """查询 Agent 自主定时任务。"""
        payload = QueryAgentTasksInput(task_id=task_id, enabled=enabled)
        tasks = await self.run_blocking(
            "db",
            self._query_tasks,
            payload.task_id,
            payload.enabled,
        )
        return json.dumps(
            {"total": len(tasks), "tasks": tasks},
            ensure_ascii=False,
            indent=2,
        )
