from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from app.db.base import DbOper
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun


class AgentTaskOper(DbOper):
    """
    Agent 自主定时任务管理。
    """

    @staticmethod
    def _now() -> str:
        """生成当前数据库时间字符串。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add(self, **kwargs: object) -> Optional[AgentTask]:
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
        删除非运行中的 Agent 定时任务及其运行历史。
        """
        return AgentTaskRun.delete_task_and_runs(
            self._db,
            task_id=task_id,
            user_id=user_id,
        )

    def begin_run(
            self,
            task_id: int,
            trigger_source: str = "scheduled",
    ) -> Optional[AgentTaskRun]:
        """
        原子创建一次运行并返回其任务快照。
        """
        run_id = uuid4().hex
        created_run_id = AgentTaskRun.begin_run(
            self._db,
            task_id=task_id,
            run_id=run_id,
            trigger_source=trigger_source,
            started_at=self._now(),
        )
        return self.get_run(created_run_id) if created_run_id else None

    def mark_running(self, task_id: int) -> bool:
        """兼容既有调用并为该次执行创建运行记录。"""
        return self.begin_run(task_id=task_id) is not None

    def mark_interrupted(self, task_id: int, result: str) -> bool:
        """
        将遗留的运行中任务标记为中断且结果未知。
        """
        return AgentTaskRun.interrupt_task(
            self._db,
            task_id=task_id,
            result=(result or "")[:20000],
            finished_at=self._now(),
        )

    def get_run(self, run_id: str) -> Optional[AgentTaskRun]:
        """查询一次 Agent 任务运行。"""
        return AgentTaskRun.get_by_run_id(self._db, run_id=run_id)

    def list_runs(
            self,
            task_id: int,
            user_id: Optional[str] = None,
            limit: int = 10,
    ) -> list[AgentTaskRun]:
        """查询任务最近的有界运行历史。"""
        return AgentTaskRun.list_for_task(
            self._db,
            task_id=task_id,
            user_id=user_id,
            limit=limit,
        )

    def finish_run(
            self,
            run_id: str,
            success: bool,
            result: str,
            disable_date_task: bool = False,
    ) -> bool:
        """收口精确运行并更新仍匹配的任务投影。"""
        return AgentTaskRun.finish_run(
            self._db,
            run_id=run_id,
            success=success,
            result=(result or "")[:20000],
            finished_at=self._now(),
            disable_date_task=disable_date_task,
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
        task = self.get(task_id)
        if not task or not task.last_run_id:
            return False
        return self.finish_run(
            run_id=task.last_run_id,
            success=success,
            result=result,
            disable_date_task=disable,
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
            "last_run_id": task.last_run_id,
            "run_count": task.run_count or 0,
            "next_run_at": next_run_at,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    @staticmethod
    def run_to_dict(run: AgentTaskRun) -> dict:
        """将一次 Agent 任务运行转换为工具返回结构。"""
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "trigger_source": run.trigger_source,
            "name": run.name,
            "content": run.content,
            "trigger_type": run.trigger_type,
            "cron_expression": run.cron_expression,
            "run_at": run.run_at,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "result": run.result,
        }
