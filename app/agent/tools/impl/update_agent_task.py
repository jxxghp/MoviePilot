import json
from datetime import datetime, timedelta
from typing import Literal, Optional, Type

import pytz
from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.config import settings
from app.db.agenttask_oper import AgentTaskOper
from app.utils.timer import TimerUtils


class UpdateAgentTaskInput(BaseModel):
    """更新 Agent 自主定时任务的输入参数。"""

    task_id: int = Field(..., ge=1, description="ID of the task to update.")
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    content: Optional[str] = Field(None, min_length=1, max_length=10000)
    trigger_type: Optional[Literal["date", "cron"]] = Field(
        None,
        description="New trigger type. Must be provided together with trigger.",
    )
    trigger: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description="New ISO 8601 date or five-field cron expression.",
    )
    delay_minutes: Optional[int] = Field(
        None,
        ge=1,
        le=525600,
        description=(
            "For a one-time date task expressed as 'in N minutes', provide this instead "
            "of trigger together with trigger_type='date'."
        ),
    )
    enabled: Optional[bool] = Field(
        None,
        description="Set false to pause the task or true to resume it.",
    )

    @model_validator(mode="after")
    def validate_update(self) -> "UpdateAgentTaskInput":
        """校验更新内容和触发参数组合。"""
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name 不能只包含空白字符")
        if self.content is not None:
            self.content = self.content.strip()
            if not self.content:
                raise ValueError("content 不能只包含空白字符")
        has_schedule_update = any(
            value is not None
            for value in (self.trigger_type, self.trigger, self.delay_minutes)
        )
        if has_schedule_update:
            if self.trigger_type is None:
                raise ValueError("修改触发配置时必须提供 trigger_type")
            if self.trigger_type == "date":
                if self.delay_minutes is not None:
                    # 保持校验幂等，具体绝对时间在更新调度前只计算一次。
                    self.trigger = None
                elif self.trigger is None:
                    raise ValueError("date 任务必须提供 trigger 或 delay_minutes")
            elif self.trigger is None or self.delay_minutes is not None:
                raise ValueError("cron 任务必须提供 trigger，且不能提供 delay_minutes")
        if all(
            value is None
            for value in (
                self.name,
                self.content,
                self.trigger_type,
                self.enabled,
            )
        ):
            raise ValueError("至少需要提供一个要更新的字段")
        return self


class UpdateAgentTaskTool(MoviePilotTool):
    """修改、暂停或恢复 Agent 自主定时任务。"""

    name: str = "update_agent_task"
    tags: list[str] = [ToolTag.Write, ToolTag.AgentTask, ToolTag.Admin]
    description: str = (
        "Update an autonomous agent task's name, instructions, exact date or cron "
        "trigger, relative delay_minutes, or enabled state. Use enabled=false to pause "
        "and enabled=true to resume."
    )
    args_schema: Type[BaseModel] = UpdateAgentTaskInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成更新定时任务的提示消息。"""
        return f"更新自主定时任务：{kwargs.get('task_id', '')}"

    def _update_task(self, payload: UpdateAgentTaskInput) -> Optional[dict]:
        """更新当前用户的任务并刷新运行时调度。"""
        from app.scheduler import Scheduler

        oper = AgentTaskOper()
        task = oper.get(task_id=payload.task_id, user_id=str(self._user_id))
        if not task:
            return None
        if task.last_status == "running":
            return {"error": f"Agent 定时任务 {payload.task_id} 正在执行，请稍后再修改"}

        trigger_type = payload.trigger_type or task.trigger_type
        trigger_value = payload.trigger
        if trigger_type == "date" and payload.delay_minutes is not None:
            timezone = pytz.timezone(settings.TZ)
            trigger_value = (
                datetime.now(timezone) + timedelta(minutes=payload.delay_minutes)
            ).isoformat(timespec="seconds")
        if trigger_value is None:
            trigger_value = (
                task.cron_expression if trigger_type == "cron" else task.run_at
            )
        enabled = task.enabled if payload.enabled is None else payload.enabled
        normalized_type, normalized_trigger = TimerUtils.normalize_schedule_trigger(
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            timezone_name=settings.TZ,
            require_future=bool(enabled and trigger_type == "date"),
        )

        update_payload = {}
        if payload.name is not None:
            update_payload["name"] = payload.name.strip()
        if payload.content is not None:
            update_payload["content"] = payload.content.strip()
        if payload.trigger_type is not None:
            update_payload.update(
                {
                    "trigger_type": normalized_type,
                    "cron_expression": (
                        normalized_trigger if normalized_type == "cron" else None
                    ),
                    "run_at": normalized_trigger if normalized_type == "date" else None,
                    "last_status": "waiting",
                    "last_result": None,
                }
            )
        if payload.enabled is not None:
            update_payload["enabled"] = payload.enabled
            if payload.enabled:
                update_payload["last_status"] = "waiting"

        oper.update(
            task_id=payload.task_id,
            payload=update_payload,
            user_id=str(self._user_id),
        )
        scheduler = Scheduler()
        next_run_at = scheduler.update_agent_task_job(payload.task_id)
        updated_task = oper.get(task_id=payload.task_id, user_id=str(self._user_id))
        return oper.to_dict(
            updated_task,
            next_run_at=next_run_at,
            timezone=settings.TZ,
        )

    async def run(
            self,
            task_id: int,
            name: Optional[str] = None,
            content: Optional[str] = None,
            trigger_type: Optional[str] = None,
            trigger: Optional[str] = None,
            delay_minutes: Optional[int] = None,
            enabled: Optional[bool] = None,
            **kwargs: object,
    ) -> str:
        """更新 Agent 自主定时任务。"""
        payload = UpdateAgentTaskInput(
            task_id=task_id,
            name=name,
            content=content,
            trigger_type=trigger_type,
            trigger=trigger,
            delay_minutes=delay_minutes,
            enabled=enabled,
        )
        task = await self.run_blocking("db", self._update_task, payload)
        if not task:
            return f"Agent 定时任务 {task_id} 不存在或不属于当前用户"
        if task.get("error"):
            return task["error"]
        return json.dumps(task, ensure_ascii=False, indent=2)
