"""Agent 自主定时任务统一工具。"""

import json
from datetime import datetime, timedelta
from typing import Literal, Optional, Type, cast

import pytz  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, model_validator

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.agenttask import agent_task_run_to_dict, agent_task_to_dict
from app.runtime.scheduling import TimerUtils
from app.runtime.settings import get_runtime_setting

AgentTaskAction = Literal["create", "list", "update", "run", "delete"]
AgentTaskTriggerType = Literal["date", "cron"]


class AgentTaskInput(BaseModel):  # type: ignore[misc]
    """统一管理 Agent 自主定时任务的输入参数。"""

    action: AgentTaskAction = Field(
        ...,
        description=(
            "Action to perform: create, list, update, run, or delete. "
            "Use list before changing a task when its integer task_id is unknown."
        ),
    )
    task_id: Optional[int] = Field(
        None,
        ge=1,
        description="Task ID for list, update, run, or delete.",
    )
    name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=100,
        description="Task name for create or update.",
    )
    content: Optional[str] = Field(
        None,
        min_length=1,
        max_length=10000,
        description="Complete instructions for create or update.",
    )
    trigger_type: Optional[AgentTaskTriggerType] = Field(
        None,
        description="Use date for one future run or cron for recurring work.",
    )
    trigger: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description="ISO 8601 date or standard five-field cron expression.",
    )
    delay_minutes: Optional[int] = Field(
        None,
        ge=1,
        le=525600,
        description=("Relative delay for a one-time date task. Use instead of trigger with trigger_type=date."),
    )
    enabled: Optional[bool] = Field(
        None,
        description=("For list, optionally filter by enabled state. For update, set false to pause or true to resume."),
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def validate_action(self) -> "AgentTaskInput":
        """按动作校验必填字段和触发参数组合。"""
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name 不能只包含空白字符")
        if self.content is not None:
            self.content = self.content.strip()
            if not self.content:
                raise ValueError("content 不能只包含空白字符")

        if self.action == "create":
            if self.name is None or self.content is None or self.trigger_type is None:
                raise ValueError("create 必须提供 name、content 和 trigger_type")
            self._validate_schedule(require_schedule=True, normalize_exact=True)
        elif self.action == "update":
            if self.task_id is None:
                raise ValueError("update 必须提供 task_id")
            has_schedule_update = any(
                value is not None for value in (self.trigger_type, self.trigger, self.delay_minutes)
            )
            if has_schedule_update:
                self._validate_schedule(require_schedule=True, normalize_exact=False)
            if all(
                value is None
                for value in (
                    self.name,
                    self.content,
                    self.trigger_type,
                    self.enabled,
                )
            ):
                raise ValueError("update 至少需要提供一个要更新的字段")
        elif self.action in {"run", "delete"} and self.task_id is None:
            raise ValueError(f"{self.action} 必须提供 task_id")
        return self

    def _validate_schedule(
        self,
        *,
        require_schedule: bool,
        normalize_exact: bool,
    ) -> None:
        """校验 date/cron 触发组合，并按需规范化绝对触发值。"""
        if require_schedule and self.trigger_type is None:
            raise ValueError("修改触发配置时必须提供 trigger_type")
        if self.trigger_type == "date":
            if self.delay_minutes is not None:
                self.trigger = None
                return
            if self.trigger is None:
                raise ValueError("date 任务必须提供 trigger 或 delay_minutes")
        elif self.trigger_type == "cron":
            if self.trigger is None or self.delay_minutes is not None:
                raise ValueError("cron 任务必须提供 trigger，且不能提供 delay_minutes")
        if normalize_exact and self.trigger_type is not None:
            normalized_type, normalized_trigger = TimerUtils.normalize_schedule_trigger(
                trigger_type=self.trigger_type,
                trigger_value=self.trigger,  # type: ignore[arg-type]
                timezone_name=get_runtime_setting("TZ"),
                require_future=True,
            )
            self.trigger_type = cast(AgentTaskTriggerType, normalized_type)
            self.trigger = normalized_trigger


class AgentTaskTool(MoviePilotTool):
    """通过一个动作式接口管理当前用户的自主定时任务。"""

    name: str = "agent_task"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Write,
        ToolTag.AgentTask,
        ToolTag.Admin,
    ]
    description: str = (
        "Manage persistent autonomous agent tasks with one structured action. Use create "
        "for explicitly requested delayed or recurring work, list to inspect current-user "
        "tasks, update to change or pause one, run to queue one immediately, and delete "
        "to permanently remove it. Task IDs are integers and are not scheduler job IDs."
    )
    args_schema: Type[BaseModel] = AgentTaskInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成统一任务操作的用户可见提示。"""
        action = str(kwargs.get("action") or "list")
        action_name = {
            "create": "创建",
            "list": "查询",
            "update": "更新",
            "run": "立即执行",
            "delete": "删除",
        }.get(action, action)
        target = kwargs.get("task_id") or kwargs.get("name") or ""
        return f"{action_name}自主定时任务：{target}".rstrip("：")

    @staticmethod
    def _absolute_trigger(payload: AgentTaskInput) -> str:
        """将相对分钟或绝对表达式转换为规范化触发值。"""
        trigger_value = payload.trigger
        if payload.trigger_type == "date" and payload.delay_minutes is not None:
            timezone = pytz.timezone(get_runtime_setting("TZ"))
            trigger_value = (datetime.now(timezone) + timedelta(minutes=payload.delay_minutes)).isoformat(
                timespec="seconds"
            )
        if payload.trigger_type is None:
            raise ValueError("Agent 定时任务缺少 trigger_type")
        if trigger_value is None:
            raise ValueError("date 任务必须提供 trigger 或 delay_minutes")
        _, normalized = TimerUtils.normalize_schedule_trigger(
            trigger_type=payload.trigger_type,
            trigger_value=trigger_value,
            timezone_name=get_runtime_setting("TZ"),
            require_future=True,
        )
        return normalized

    def _create_task(self, payload: AgentTaskInput) -> dict[str, object]:
        """持久化任务并立即注册到运行时调度器。"""
        from app.application.scheduling import update_agent_task_job

        trigger_value = self._absolute_trigger(payload)
        if payload.name is None or payload.content is None or payload.trigger_type is None:
            raise ValueError("create 缺少必要参数")
        chat = self.data.chat.get_sync(
            session_id=self._session_id,
            user_id=self._user_id,
        )
        task = self.data.tasks.add(
            name=payload.name,
            content=payload.content,
            trigger_type=payload.trigger_type,
            cron_expression=(trigger_value if payload.trigger_type == "cron" else None),
            run_at=trigger_value if payload.trigger_type == "date" else None,
            user_id=str(self._user_id),
            username=self._username or (chat.username if chat else None),
            session_id=str(self._session_id),
            channel=self._channel or (chat.channel if chat else None),
            source=self._source or (chat.source if chat else None),
            original_chat_id=chat.original_chat_id if chat else None,
        )
        if task is None:
            raise RuntimeError("Agent 定时任务创建后无法读取")
        return agent_task_to_dict(
            task,
            next_run_at=update_agent_task_job(task.id),
            timezone=get_runtime_setting("TZ"),
        )

    def _list_tasks(
        self,
        task_id: Optional[int],
        enabled: Optional[bool],
    ) -> list[dict[str, object]]:
        """读取当前用户任务及运行时下一次触发时间。"""
        from app.application.scheduling import get_agent_task_next_run

        oper = self.data.tasks
        if task_id is not None:
            task = oper.get(task_id=task_id, user_id=str(self._user_id))
            tasks = [task] if task else []
        else:
            tasks = oper.list(user_id=str(self._user_id), enabled=enabled)
        result: list[dict[str, object]] = []
        for task in tasks:
            data = agent_task_to_dict(
                task,
                next_run_at=get_agent_task_next_run(task.id),
                timezone=get_runtime_setting("TZ"),
            )
            if task_id is not None:
                data["recent_runs"] = [
                    agent_task_run_to_dict(run)
                    for run in oper.list_runs(
                        task_id=task.id,
                        user_id=str(self._user_id),
                        limit=10,
                    )
                ]
            result.append(data)
        return result

    def _update_task(
        self,
        payload: AgentTaskInput,
    ) -> Optional[dict[str, object]]:
        """更新当前用户任务并刷新运行时调度。"""
        from app.application.scheduling import update_agent_task_job

        if payload.task_id is None:
            raise ValueError("update 缺少 task_id")
        oper = self.data.tasks
        task = oper.get(task_id=payload.task_id, user_id=str(self._user_id))
        if not task:
            return None
        if task.last_status == "running":
            return {"error": f"Agent 定时任务 {payload.task_id} 正在执行，请稍后再修改"}

        has_schedule_update = payload.trigger_type is not None
        trigger_type = payload.trigger_type or task.trigger_type
        trigger_value = payload.trigger
        if trigger_type == "date" and payload.delay_minutes is not None:
            timezone = pytz.timezone(get_runtime_setting("TZ"))
            trigger_value = (datetime.now(timezone) + timedelta(minutes=payload.delay_minutes)).isoformat(
                timespec="seconds"
            )
        if trigger_value is None:
            trigger_value = task.cron_expression if trigger_type == "cron" else task.run_at
        normalized_type = trigger_type
        normalized_trigger = trigger_value
        validates_existing_date_schedule = bool(
            payload.enabled and task.last_status != "interrupted" and trigger_type == "date"
        )
        if has_schedule_update or validates_existing_date_schedule:
            if trigger_value is None:
                raise ValueError("Agent 定时任务缺少触发配置")
            normalized_type, normalized_trigger = TimerUtils.normalize_schedule_trigger(
                trigger_type=trigger_type,
                trigger_value=trigger_value,
                timezone_name=get_runtime_setting("TZ"),
                require_future=bool(
                    trigger_type == "date"
                    and (
                        task.last_status == "interrupted"
                        or (task.enabled if payload.enabled is None else payload.enabled)
                    )
                ),
            )

        update_payload: dict[str, object] = {}
        if payload.name is not None:
            update_payload["name"] = payload.name
        if payload.content is not None:
            update_payload["content"] = payload.content
        if has_schedule_update:
            update_payload.update(
                {
                    "trigger_type": normalized_type,
                    "cron_expression": (normalized_trigger if normalized_type == "cron" else None),
                    "run_at": (normalized_trigger if normalized_type == "date" else None),
                    "last_status": "waiting",
                    "last_result": None,
                }
            )
        if payload.enabled is not None:
            update_payload["enabled"] = payload.enabled
            if payload.enabled and task.last_status != "interrupted":
                update_payload["last_status"] = "waiting"

        updated = oper.update(
            task_id=payload.task_id,
            payload=update_payload,
            user_id=str(self._user_id),
        )
        if not updated:
            current = oper.get(task_id=payload.task_id, user_id=str(self._user_id))
            if current and current.last_status == "running":
                return {"error": (f"Agent 定时任务 {payload.task_id} 正在执行，请稍后再修改")}
            return None
        updated_task = oper.get(
            task_id=payload.task_id,
            user_id=str(self._user_id),
        )
        if updated_task is None:
            return None
        return agent_task_to_dict(
            updated_task,
            next_run_at=update_agent_task_job(payload.task_id),
            timezone=get_runtime_setting("TZ"),
        )

    def _get_task_state(self, task_id: int) -> tuple[str, Optional[str]]:
        """校验任务归属和状态，返回可执行性及任务名称。"""
        task = self.data.tasks.get(task_id=task_id, user_id=str(self._user_id))
        if not task:
            return "not_found", None
        if not task.enabled:
            return "disabled", task.name
        if task.last_status == "running":
            return "running", task.name
        return "ready", task.name

    def _delete_task(self, task_id: int) -> bool:
        """删除当前用户任务并移除运行时调度。"""
        from app.application.scheduling import remove_agent_task_job

        deleted = self.data.tasks.delete(
            task_id=task_id,
            user_id=str(self._user_id),
        )
        if deleted:
            remove_agent_task_job(task_id)
        return deleted

    async def run(  # type: ignore[override]
        self,
        action: AgentTaskAction,
        task_id: Optional[int] = None,
        name: Optional[str] = None,
        content: Optional[str] = None,
        trigger_type: Optional[AgentTaskTriggerType] = None,
        trigger: Optional[str] = None,
        delay_minutes: Optional[int] = None,
        enabled: Optional[bool] = None,
        **kwargs: object,
    ) -> str:
        """执行一项任务创建、查询、更新、立即运行或删除操作。"""
        payload = AgentTaskInput(
            action=action,
            task_id=task_id,
            name=name,
            content=content,
            trigger_type=trigger_type,
            trigger=trigger,
            delay_minutes=delay_minutes,
            enabled=enabled,
        )
        if payload.action == "create":
            if not get_runtime_setting("AI_AGENT_ENABLE"):
                return "AI Agent 未启用，无法创建自主定时任务"
            task = await self.run_blocking("db", self._create_task, payload)
            return json.dumps(task, ensure_ascii=False, indent=2)
        if payload.action == "list":
            tasks = await self.run_blocking(
                "db",
                self._list_tasks,
                payload.task_id,
                payload.enabled,
            )
            return json.dumps(
                {"total": len(tasks), "tasks": tasks},
                ensure_ascii=False,
                indent=2,
            )
        if payload.action == "update":
            task = await self.run_blocking("db", self._update_task, payload)
            if not task:
                return f"Agent 定时任务 {payload.task_id} 不存在或不属于当前用户"
            if error := task.get("error"):
                return str(error)
            return json.dumps(task, ensure_ascii=False, indent=2)
        if payload.action == "run":
            return await self._run_now(payload.task_id)
        return await self._delete(payload.task_id)

    async def _run_now(self, task_id: Optional[int]) -> str:
        """立即提交一项已启用且属于当前用户的任务。"""
        from app.application.scheduling import start_agent_task

        if task_id is None:
            raise ValueError("run 缺少 task_id")
        status, task_name = await self.run_blocking(
            "db",
            self._get_task_state,
            task_id,
        )
        if status == "not_found":
            return f"Agent 定时任务 {task_id} 不存在或不属于当前用户"
        if status == "disabled":
            return f"Agent 定时任务 {task_id} 已暂停，请先恢复后再执行"
        if status == "running":
            return f"Agent 定时任务 {task_id} 正在执行，请勿重复触发"
        if not start_agent_task(task_id):
            return f"Agent 定时任务 {task_id} 尚未注册到运行时调度器，无法立即执行"
        return f"Agent 定时任务 {task_id} 已提交立即执行：{task_name}。执行完成后将通过已配置的通知渠道广播结果"

    async def _delete(self, task_id: Optional[int]) -> str:
        """永久删除一项属于当前用户的自主定时任务。"""
        if task_id is None:
            raise ValueError("delete 缺少 task_id")
        deleted = await self.run_blocking("db", self._delete_task, task_id)
        if not deleted:
            return f"Agent 定时任务 {task_id} 不存在或不属于当前用户"
        return f"Agent 定时任务 {task_id} 已删除"
