import json
from datetime import datetime, timedelta
from typing import Literal, Optional, Type

import pytz
from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.config import settings
from app.db.agentchat_oper import AgentChatOper
from app.db.agenttask_oper import AgentTaskOper
from app.utils.timer import TimerUtils


class CreateAgentTaskInput(BaseModel):
    """创建 Agent 自主定时任务的输入参数。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Short task name shown in task management and execution reports.",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="Complete instructions that the agent must execute when the task fires.",
    )
    trigger_type: Literal["date", "cron"] = Field(
        ...,
        description="Use 'date' for one exact future run or 'cron' for recurring work.",
    )
    trigger: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description=(
            "For date, an ISO 8601 local or timezone-aware time such as "
            "2026-07-19 20:30:00; for cron, a standard five-field expression "
            "(minute hour day month weekday). The MoviePilot system timezone is used."
        ),
    )
    delay_minutes: Optional[int] = Field(
        None,
        ge=1,
        le=525600,
        description=(
            "For a one-time date task expressed as 'in N minutes', provide this instead "
            "of trigger. MoviePilot calculates and persists the exact future run time."
        ),
    )

    @model_validator(mode="after")
    def validate_trigger(self) -> "CreateAgentTaskInput":
        """校验任务触发配置并统一格式。"""
        self.name = self.name.strip()
        self.content = self.content.strip()
        if not self.name or not self.content:
            raise ValueError("name 和 content 不能只包含空白字符")
        if self.trigger_type == "date":
            if self.delay_minutes is not None:
                # LangChain 会在 run() 前后各校验一次，延迟时间在持久化前统一计算。
                self.trigger = None
                return self
            if self.trigger is None:
                raise ValueError("date 任务必须提供 trigger 或 delay_minutes")
        elif self.trigger is None or self.delay_minutes is not None:
            raise ValueError("cron 任务必须提供 trigger，且不能提供 delay_minutes")
        self.trigger_type, self.trigger = TimerUtils.normalize_schedule_trigger(
            trigger_type=self.trigger_type,
            trigger_value=self.trigger,
            timezone_name=settings.TZ,
            require_future=True,
        )
        return self


class CreateAgentTaskTool(MoviePilotTool):
    """创建可精确唤醒当前 Agent 会话的自主定时任务。"""

    name: str = "create_agent_task"
    tags: list[str] = [ToolTag.Write, ToolTag.AgentTask, ToolTag.Admin]
    description: str = (
        "Create a persistent autonomous agent task only when the user explicitly asks "
        "for delayed, scheduled, recurring, reminder, or monitoring work. Use trigger_type "
        "'date' with delay_minutes for requests such as 'check in 30 minutes', an exact "
        "trigger time for other one-time work, and 'cron' for recurring schedules. When "
        "fired, MoviePilot wakes the agent in this conversation, executes content, and "
        "broadcasts user-facing messages through the configured notification channels."
    )
    args_schema: Type[BaseModel] = CreateAgentTaskInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成创建定时任务的提示消息。"""
        return f"创建自主定时任务：{kwargs.get('name', '')}"

    def _create_task(self, payload: CreateAgentTaskInput) -> dict:
        """持久化任务并立即注册到运行时调度器。"""
        from app.scheduler import Scheduler

        trigger_value = payload.trigger
        if payload.trigger_type == "date" and payload.delay_minutes is not None:
            timezone = pytz.timezone(settings.TZ)
            trigger_value = (
                datetime.now(timezone) + timedelta(minutes=payload.delay_minutes)
            ).isoformat(timespec="seconds")
        _, trigger_value = TimerUtils.normalize_schedule_trigger(
            trigger_type=payload.trigger_type,
            trigger_value=trigger_value,
            timezone_name=settings.TZ,
            require_future=True,
        )
        chat = AgentChatOper().get(
            session_id=self._session_id,
            user_id=self._user_id,
        )
        task = AgentTaskOper().add(
            name=payload.name.strip(),
            content=payload.content.strip(),
            trigger_type=payload.trigger_type,
            cron_expression=trigger_value if payload.trigger_type == "cron" else None,
            run_at=trigger_value if payload.trigger_type == "date" else None,
            user_id=str(self._user_id),
            username=self._username or (chat.username if chat else None),
            session_id=str(self._session_id),
            channel=self._channel or (chat.channel if chat else None),
            source=self._source or (chat.source if chat else None),
            original_chat_id=chat.original_chat_id if chat else None,
        )
        scheduler = Scheduler()
        next_run_at = scheduler.update_agent_task_job(task.id)
        return AgentTaskOper.to_dict(
            task,
            next_run_at=next_run_at,
            timezone=settings.TZ,
        )

    async def run(
            self,
            name: str,
            content: str,
            trigger_type: str,
            trigger: Optional[str] = None,
            delay_minutes: Optional[int] = None,
            **kwargs: object,
    ) -> str:
        """创建 Agent 自主定时任务。"""
        if not settings.AI_AGENT_ENABLE:
            return "AI Agent 未启用，无法创建自主定时任务"
        payload = CreateAgentTaskInput(
            name=name,
            content=content,
            trigger_type=trigger_type,
            trigger=trigger,
            delay_minutes=delay_minutes,
        )
        task = await self.run_blocking("db", self._create_task, payload)
        return json.dumps(task, ensure_ascii=False, indent=2)
