"""Agent 自主定时任务的调度登记。

本模块只负责把 Agent 任务翻译成调度器登记项，作业执行状态、锁与调度器实例
由组合根持有，混入类通过 ``self`` 取用。
"""

from typing import Optional

from apscheduler.jobstores.base import JobLookupError

from app.application.scheduling import AGENT_TASK_JOB_PREFIX
from app.db.oper.agenttask import AgentTaskOper
from app.runtime.config import settings
from app.runtime.log import logger
from app.runtime.scheduling import TimerUtils


class AgentTaskScheduling:
    """Agent 自主定时任务的注册、对账、查询与执行。"""

    @staticmethod
    def _get_agent_task_job_id(task_id: int) -> str:
        """生成 Agent 自主定时任务的调度器 Job ID。"""
        return f"{AGENT_TASK_JOB_PREFIX}-{task_id}"

    def start_agent_task(self, task_id: int) -> bool:
        """
        将指定 Agent 自主定时任务提交到运行时调度器立即执行。

        :param task_id: Agent 自主定时任务 ID
        :return: 任务存在且未运行时返回 True，否则返回 False
        """
        job_id = self._get_agent_task_job_id(task_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.get("running"):
                return False
        self.start(job_id, task_id=task_id, trigger_source="manual")
        return True

    def init_agent_task_jobs(self) -> None:
        """
        按数据库当前状态注册所有启用的 Agent 自主定时任务。
        """
        for task in AgentTaskOper().list(enabled=True):
            self.update_agent_task_job(task.id)

    def _reconcile_agent_task_interruptions(self) -> None:
        """
        将上个进程未收口的 Agent 任务标记为结果未知。

        配置变更会在同一进程内重建调度器，因此该对账在实例生命周期内只能
        成功执行一次，避免把当前进程仍在运行的任务误判为中断。
        """
        with self._lock:
            if self._agent_task_interruptions_reconciled:
                return
            oper = AgentTaskOper()
            for task in oper.list():
                if task.last_status == "running":
                    oper.mark_interrupted(
                        task_id=task.id,
                        result=(
                            "服务重启时任务执行被中断，结果未知，可能已有部分操作；"
                            "请先检查实际状态，再决定是否重新执行"
                        ),
                    )
            self._agent_task_interruptions_reconciled = True

    def update_agent_task_job(self, task_id: int) -> Optional[str]:
        """
        按数据库中的最新配置新增或替换 Agent 自主定时任务。

        :param task_id: Agent 定时任务 ID
        :return: 下一次执行时间，不可调度时返回 None
        """
        self.remove_agent_task_job(task_id)
        task = AgentTaskOper().get(task_id)
        if (
                not settings.AI_AGENT_ENABLE
                or not task
                or not task.enabled
                or not self._scheduler
        ):
            return None

        trigger_value = (
            task.cron_expression if task.trigger_type == "cron" else task.run_at
        )
        manual_only = task.trigger_type == "date" and task.last_status == "interrupted"
        trigger = None
        if not manual_only:
            try:
                trigger = TimerUtils.build_schedule_trigger(
                    trigger_type=task.trigger_type,
                    trigger_value=trigger_value,
                    timezone_name=settings.TZ,
                )
            except (TypeError, ValueError) as err:
                logger.error(f"Agent 定时任务 {task_id} 的触发配置无效：{str(err)}")
                return None

        job_id = self._get_agent_task_job_id(task_id)
        with self._lock:
            self._jobs[job_id] = {
                "name": task.name,
                "provider_name": "[Agent]",
                "func": self.execute_agent_task,
                "running": False,
                "kwargs": {"task_id": task_id},
            }
            # 已开始的一次任务在重启后结果未知，只保留显式执行入口，不能按
            # 过期触发时间自动重放可能已经发生的外部副作用。
            if manual_only:
                return None
            self._scheduler.add_job(
                self.start,
                trigger=trigger,
                id=job_id,
                name=task.name,
                kwargs={"job_id": job_id, "task_id": task_id},
                coalesce=True,
                max_instances=1,
                misfire_grace_time=None,
                replace_existing=True,
            )
        return self.get_agent_task_next_run(task_id)

    def remove_agent_task_job(self, task_id: int) -> None:
        """
        从运行时调度器移除 Agent 自主定时任务。

        :param task_id: Agent 定时任务 ID
        """
        job_id = self._get_agent_task_job_id(task_id)
        with self._lock:
            self._jobs.pop(job_id, None)
            if not self._scheduler:
                return
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                pass

    def get_agent_task_next_run(self, task_id: int) -> Optional[str]:
        """
        查询 Agent 自主定时任务的下一次执行时间。

        :param task_id: Agent 定时任务 ID
        :return: 带时区的 ISO 8601 时间，不再执行时返回 None
        """
        job_id = self._get_agent_task_job_id(task_id)
        if self._scheduler:
            job = self._scheduler.get_job(job_id)
            next_run_time = getattr(job, "next_run_time", None) if job else None
            if next_run_time:
                return next_run_time.isoformat(timespec="seconds")

        task = AgentTaskOper().get(task_id)
        if not task or not task.enabled:
            return None
        if task.trigger_type == "date" and task.last_status == "interrupted":
            return None
        trigger_value = (
            task.cron_expression if task.trigger_type == "cron" else task.run_at
        )
        try:
            next_run_time = TimerUtils.get_schedule_next_run_time(
                trigger_type=task.trigger_type,
                trigger_value=trigger_value,
                timezone_name=settings.TZ,
            )
        except (TypeError, ValueError):
            return None
        return (
            next_run_time.isoformat(timespec="seconds")
            if next_run_time
            else None
        )

    async def execute_agent_task(
            self,
            task_id: int,
            trigger_source: str = "scheduled",
    ) -> tuple[bool, str]:
        """
        唤醒 Agent 执行指定自主定时任务。

        :param task_id: Agent 定时任务 ID
        :param trigger_source: 触发入口，scheduled-自动调度，manual-显式立即执行
        :return: 执行是否成功及结果摘要
        """
        from app.agent.runtime_loader import get_running_agent_manager

        try:
            manager = get_running_agent_manager()
            if manager is None:
                logger.warning("智能助手服务未运行，跳过 Agent 定时任务")
                return False, "智能助手服务未运行"
            return await manager.execute_scheduled_task(
                task_id,
                trigger_source=trigger_source,
            )
        finally:
            task = AgentTaskOper().get(task_id)
            if task and task.trigger_type == "date" and not task.enabled:
                self.remove_agent_task_job(task_id)
