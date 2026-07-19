from datetime import datetime
from typing import Optional

from app.db import DbOper
from app.db.models.agenttask import AgentTask


class AgentTaskOper(DbOper):
    """
    Agent 自主定时任务管理。
    """

    @staticmethod
    def _now() -> str:
        """生成当前数据库时间字符串。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add(self, **kwargs: object) -> AgentTask:
        """
        新增 Agent 定时任务。
        """
        now = self._now()
        task_id = AgentTask.add_task(
            self._db,
            **kwargs,
            enabled=True,
            last_status="waiting",
            run_count=0,
            created_at=now,
            updated_at=now,
        )
        return self.get(task_id)

    def get(
            self,
            task_id: int,
            user_id: Optional[str] = None,
    ) -> Optional[AgentTask]:
        """
        查询单个 Agent 定时任务。
        """
        return AgentTask.get_for_user(self._db, task_id=task_id, user_id=user_id)

    def list(
            self,
            user_id: Optional[str] = None,
            enabled: Optional[bool] = None,
    ) -> list[AgentTask]:
        """
        查询 Agent 定时任务列表。
        """
        return AgentTask.list_for_user(self._db, user_id=user_id, enabled=enabled)

    def update(
            self,
            task_id: int,
            payload: dict,
            user_id: Optional[str] = None,
    ) -> bool:
        """
        更新 Agent 定时任务。
        """
        normalized_payload = {
            key: value
            for key, value in payload.items()
            if key in {
                "name",
                "content",
                "trigger_type",
                "cron_expression",
                "run_at",
                "enabled",
                "last_status",
                "last_result",
            }
        }
        if not normalized_payload:
            return False
        normalized_payload["updated_at"] = self._now()
        return AgentTask.update_task(
            self._db,
            task_id=task_id,
            payload=normalized_payload,
            user_id=user_id,
        )

    def delete(self, task_id: int, user_id: Optional[str] = None) -> bool:
        """
        删除 Agent 定时任务。
        """
        return AgentTask.delete_task(
            self._db,
            task_id=task_id,
            user_id=user_id,
        )

    def mark_running(self, task_id: int) -> bool:
        """
        将 Agent 定时任务标记为运行中。
        """
        return AgentTask.mark_running(
            self._db,
            task_id=task_id,
            run_at=self._now(),
        )

    def finish(
            self,
            task_id: int,
            success: bool,
            result: str,
            disable: bool = False,
    ) -> bool:
        """
        记录 Agent 定时任务执行结果。
        """
        return AgentTask.finish_task(
            self._db,
            task_id=task_id,
            success=success,
            result=(result or "")[:20000],
            disable=disable,
        )

    @staticmethod
    def to_dict(
            task: AgentTask,
            next_run_at: Optional[str] = None,
            timezone: Optional[str] = None,
    ) -> dict:
        """
        将 Agent 定时任务转换为工具可返回的结构。
        """
        return {
            "id": task.id,
            "name": task.name,
            "content": task.content,
            "trigger_type": task.trigger_type,
            "cron_expression": task.cron_expression,
            "run_at": task.run_at,
            "timezone": timezone,
            "enabled": bool(task.enabled),
            "last_status": task.last_status,
            "last_run_at": task.last_run_at,
            "last_result": task.last_result,
            "run_count": task.run_count or 0,
            "next_run_at": next_run_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
