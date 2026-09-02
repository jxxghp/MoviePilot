"""Agent、插件和工作流动态任务对账。"""

import threading
import traceback
from typing import Any, Optional, cast

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger

from app.application.agenttask import AgentTaskRepository
from app.application.configuration import (
    get_configured_system_config,
    get_scheduler_runtime_config,
)
from app.application.plugin.routes import register_plugin_api
from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import (  # noqa: E402
    AGENT_TASK_JOB_PREFIX,
    JobRecoveryPolicy,
    JobSpec,
)
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.workflow import WorkflowSnapshot
from app.runtime.log import logger
from app.runtime.scheduling import TimerUtils
from app.scheduler.contract import _SchedulerOwnerBase
from app.schemas.message import Message
from app.schemas.types import MessageType, SystemConfigKey


class SchedulerReconcileOwner(_SchedulerOwnerBase):
    """Agent、插件和工作流动态任务对账。"""

    def configure_agent_tasks(self, repository: AgentTaskRepository) -> None:
        """在调度器启动前绑定唯一自主任务仓储。"""
        if self._lifecycle_state not in {"new", "stopped"}:
            raise RuntimeError("Scheduler 已运行，不能替换 AgentTask 仓储")
        self._agent_tasks = repository

    def _agent_task_repository(self) -> AgentTaskRepository:
        """返回显式注入仓储；缺少组合根装配时稳定失败。"""
        if self._agent_tasks is None:
            raise RuntimeError("Scheduler 的 AgentTask 仓储尚未注入")
        return self._agent_tasks

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
        owner = threading.get_ident()
        with self._lock:
            job = self._jobs.get(job_id)
            if not self._accepting_submissions() or not job or self._is_job_active(job_id) or job.get("running"):
                return False
            if not self._registry.reserve(job_id, owner):
                return False
        try:
            result = self.start(job_id, task_id=task_id, trigger_source="manual")
            return result is not False
        finally:
            self._registry.release_reservation(job_id, owner)

    def init_agent_task_jobs(self) -> None:
        """
        按数据库当前状态注册所有启用的 Agent 自主定时任务。
        """
        for task in self._agent_task_repository().list(enabled=True):
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
            oper = self._agent_task_repository()
            for task in oper.list():
                if task.last_status == "running":
                    oper.mark_interrupted(
                        task_id=task.id,
                        result=(
                            "服务重启时任务执行被中断，结果未知，可能已有部分操作；请先检查实际状态，再决定是否重新执行"
                        ),
                    )
            self._agent_task_interruptions_reconciled = True

    def update_agent_task_job(self, task_id: int) -> Optional[str]:
        """
        按数据库中的最新配置新增或替换 Agent 自主定时任务。

        :param task_id: Agent 定时任务 ID
        :return: 下一次执行时间，不可调度时返回 None
        """
        config = get_scheduler_runtime_config()
        self.remove_agent_task_job(task_id)
        task = self._agent_task_repository().get(task_id)
        if not config.ai_agent_enable or not task or not task.enabled or not self._scheduler:
            return None

        trigger_value = task.cron_expression if task.trigger_type == "cron" else task.run_at
        if trigger_value is None:
            logger.error(f"Agent 定时任务 {task_id} 缺少触发配置")
            return None
        manual_only = task.trigger_type == "date" and task.last_status == "interrupted"
        trigger = None
        if not manual_only:
            try:
                trigger = TimerUtils.build_schedule_trigger(
                    trigger_type=task.trigger_type,
                    trigger_value=trigger_value,
                    timezone_name=config.timezone,
                )
            except (TypeError, ValueError) as err:
                logger.error(f"Agent 定时任务 {task_id} 的触发配置无效：{str(err)}")
                return None

        job_id = self._get_agent_task_job_id(task_id)
        with self._lock:
            job = JobSpec(
                job_id,
                task.name,
                self.execute_agent_task,
                "agent",
                recovery=JobRecoveryPolicy.NEXT_SCHEDULE,
                kwargs={"task_id": task_id},
            ).to_runtime_state()
            self._assign_job_generation(job_id, job)
            job["_agent_task_run_id"] = task.last_run_id
            job["_agent_task_status"] = task.last_status
            self._jobs[job_id] = job
            self._jobs[job_id]["provider_name"] = "[Agent]"
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

    def _remove_agent_task_job_generation(
        self,
        task_id: int,
        generation: int,
        run_id: str,
    ) -> bool:
        """移除本次执行或其运行中重载产生的 AgentTask 调度注册。"""
        job_id = self._get_agent_task_job_id(task_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.get("_generation", 0) != generation and not (
                job.get("_agent_task_run_id") == run_id and job.get("_agent_task_status") == "running"
            ):
                return False
            self._jobs.pop(job_id, None)
            if self._scheduler:
                try:
                    self._scheduler.remove_job(job_id)
                except JobLookupError:
                    pass
        return True

    def get_agent_task_next_run(self, task_id: int) -> Optional[str]:
        """
        查询 Agent 自主定时任务的下一次执行时间。

        :param task_id: Agent 定时任务 ID
        :return: 带时区的 ISO 8601 时间，不再执行时返回 None
        """
        config = get_scheduler_runtime_config()
        job_id = self._get_agent_task_job_id(task_id)
        if self._scheduler:
            job = self._scheduler.get_job(job_id)
            next_run_time = getattr(job, "next_run_time", None) if job else None
            if next_run_time:
                return cast(str, next_run_time.isoformat(timespec="seconds"))

        task = self._agent_task_repository().get(task_id)
        if not task or not task.enabled:
            return None
        if task.trigger_type == "date" and task.last_status == "interrupted":
            return None
        trigger_value = task.cron_expression if task.trigger_type == "cron" else task.run_at
        if trigger_value is None:
            return None
        try:
            next_run_time = TimerUtils.get_schedule_next_run_time(
                trigger_type=task.trigger_type,
                trigger_value=trigger_value,
                timezone_name=config.timezone,
            )
        except (TypeError, ValueError):
            return None
        return next_run_time.isoformat(timespec="seconds") if next_run_time else None

    async def execute_agent_task(
        self,
        task_id: int,
        trigger_source: str = "scheduled",
        scheduler_generation: int | None = None,
    ) -> tuple[bool, str]:
        """
        唤醒 Agent 执行指定自主定时任务。

        :param task_id: Agent 定时任务 ID
        :param trigger_source: 触发入口，scheduled-自动调度，manual-显式立即执行
        :return: 执行是否成功及结果摘要
        """
        from app.application.agent import get_running_agent_manager

        manager = get_running_agent_manager()
        if manager is None:
            logger.warning("智能助手服务未运行，跳过 Agent 定时任务")
            return False, "智能助手服务未运行"
        if scheduler_generation is None:
            job_id = self._get_agent_task_job_id(task_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    scheduler_generation = job.get("_generation", 0)
        kwargs: dict[str, Any] = {"trigger_source": trigger_source}
        if scheduler_generation is not None:
            kwargs.update(
                scheduler_generation=scheduler_generation,
                remove_schedule=self._remove_agent_task_job_generation,
            )
        return cast(
            tuple[bool, str],
            await manager.execute_scheduled_task(task_id, **kwargs),
        )

    def init_plugin_jobs(self) -> None:
        """
        初始化插件定时服务
        """
        for pid in get_plugin_manager().get_running_plugin_ids():
            self.update_plugin_job(pid)

    def init_workflow_jobs(self) -> None:
        """
        初始化工作流定时服务
        """
        for workflow in self._scheduler_services().list_workflows() or []:
            self.update_workflow_job(workflow)

    def remove_workflow_job(self, workflow: WorkflowSnapshot) -> None:
        """
        移除工作流服务
        """
        if not self._scheduler:
            return
        with self._lock:
            job_id = f"workflow-{workflow.id}"
            service = self._jobs.pop(job_id, {})
            if not service:
                return
            try:
                # 在调度器中查找并移除对应的 job
                job_removed = False
                for job in list(self._scheduler.get_jobs()):
                    if job_id == job.id:
                        try:
                            self._scheduler.remove_job(job.id)
                            job_removed = True
                        except JobLookupError:
                            pass
                        break
                if job_removed:
                    logger.info(f"移除工作流服务：{service.get('name')}")
            except Exception as e:
                logger.error(f"移除工作流服务失败：{str(e)} - {job_id}: {service}")
                self._scheduler_services().put_message(
                    title=f"工作流 {workflow.name} 服务移除失败",
                    message=str(e),
                    role="system",
                )

    def remove_plugin_job(self, pid: str, job_id: Optional[str] = None) -> None:
        """
        移除定时服务，可以是单个服务（包括默认服务）或整个插件的所有服务
        :param pid: 插件 ID
        :param job_id: 可选，指定要移除的单个服务的 job_id。如果不提供，则移除该插件的所有服务，当移除单个服务时，默认服务也包含在内
        """
        if not self._scheduler:
            return
        with self._lock:
            if job_id:
                # 移除单个服务
                service = self._jobs.pop(job_id, None)
                if not service:
                    return
                jobs_to_remove = [(job_id, service)]
            else:
                # 移除插件的所有服务
                jobs_to_remove = [
                    (job_id, service) for job_id, service in self._jobs.items() if service.get("pid") == pid
                ]
                for job_id, _ in jobs_to_remove:
                    self._jobs.pop(job_id, None)
            if not jobs_to_remove:
                return
            plugin_name = get_plugin_manager().get_plugin_attr(pid, "plugin_name")
            # 遍历移除任务
            for job_id, service in jobs_to_remove:
                try:
                    # 在调度器中查找并移除对应的 job
                    job_removed = False
                    for job in list(self._scheduler.get_jobs()):
                        job_id_from_service = job.id.split("|")[0]
                        if job_id == job_id_from_service:
                            try:
                                self._scheduler.remove_job(job.id)
                                job_removed = True
                            except JobLookupError:
                                pass
                    if job_removed:
                        logger.info(f"移除插件服务({plugin_name})：{service.get('name')}")  # noqa
                except Exception as e:
                    logger.error(f"移除插件服务失败：{str(e)} - {job_id}: {service}")
                    self._scheduler_services().put_message(
                        title=f"插件 {plugin_name} 服务移除失败",
                        message=str(e),
                        role="system",
                    )

    def update_workflow_job(self, workflow: WorkflowSnapshot) -> None:
        """
        更新工作流定时服务
        """
        if not self._scheduler:
            return
        # 移除该工作流的全部服务
        self.remove_workflow_job(workflow)
        # 添加工作流服务
        with self._lock:
            try:
                job_id = f"workflow-{workflow.id}"
                job = JobSpec(
                    job_id,
                    workflow.name,
                    self._scheduler_services().process_workflow,
                    "workflow",
                ).to_runtime_state()
                self._assign_job_generation(job_id, job)
                job["provider_name"] = "工作流"
                self._jobs[job_id] = job
                self._scheduler.add_job(
                    self.start,
                    trigger=CronTrigger.from_crontab(workflow.timer),
                    id=job_id,
                    name=workflow.name,
                    kwargs={"job_id": job_id, "workflow_id": workflow.id},
                    replace_existing=True,
                )
                logger.info(f"注册工作流服务：{workflow.name} - {workflow.timer}")
            except Exception as e:
                logger.error(f"注册工作流服务失败：{workflow.name} - {str(e)}")
                self._scheduler_services().put_message(
                    title=f"工作流 {workflow.name} 服务注册失败",
                    message=str(e),
                    role="system",
                )

    def update_plugin_job(self, pid: str) -> None:
        """
        更新插件定时服务
        """
        if not self._scheduler or not pid:
            return
        # 移除该插件的全部服务
        self.remove_plugin_job(pid)
        # 获取插件服务列表
        with self._lock:
            plugin_manager = get_plugin_manager()
            try:
                plugin_services = plugin_manager.get_plugin_services(pid=pid)
            except Exception as e:
                logger.error(f"运行插件 {pid} 服务失败：{str(e)} - {traceback.format_exc()}")
                return
            # 获取插件名称
            plugin_name = plugin_manager.get_plugin_attr(pid, "plugin_name")
            # 开始注册插件服务
            for service in plugin_services:
                try:
                    sid = f"{pid}_{service['id']}"
                    job_id = sid.split("|")[0]
                    self.remove_plugin_job(pid, job_id)
                    job = JobSpec(
                        job_id,
                        service["name"],
                        service["func"],
                        f"plugin:{pid}",
                        kwargs=service.get("func_kwargs") or {},
                    ).to_runtime_state()
                    self._assign_job_generation(job_id, job)
                    job.update(
                        pid=pid,
                        provider_name=plugin_name,
                    )
                    self._jobs[job_id] = job
                    self._scheduler.add_job(
                        self.start,
                        service["trigger"],
                        id=sid,
                        name=service["name"],
                        **(service.get("kwargs") or {}),
                        kwargs={"job_id": job_id},
                        replace_existing=True,
                    )
                    logger.info(f"注册插件{plugin_name}服务：{service['name']} - {service['trigger']}")
                except Exception as e:
                    logger.error(f"注册插件{plugin_name}服务失败：{str(e)} - {service}")
                    self._scheduler_services().put_message(
                        title=f"插件 {plugin_name} 服务注册失败",
                        message=str(e),
                        role="system",
                    )

    def user_auth(self) -> None:
        """
        用户认证检查
        """
        config = get_scheduler_runtime_config()
        if SitesHelper().auth_level >= 2:
            if self._auth_plugin_routes_pending:
                register_plugin_api()
                self._auth_plugin_routes_pending = False
            return
        # 最大重试次数
        __max_try__ = 30
        if self._auth_count > __max_try__:
            if not self._auth_message:
                self._scheduler_services().put_message(
                    title="用户认证失败",
                    message="用户认证失败次数过多，将不再尝试认证！",
                    role="system",
                )
                self._auth_message = True
            return
        logger.info("用户未认证，正在尝试认证...")
        auth_conf = get_configured_system_config().get(SystemConfigKey.UserSiteAuthParams)
        if auth_conf:
            status, msg = SitesHelper().check_user(**auth_conf)
        else:
            status, msg = SitesHelper().check_user()
        if status:
            self._auth_count = 0
            logger.info(f"{msg} 用户认证成功")
            self._scheduler_services().post_message(
                Message(
                    mtype=MessageType.Manual,
                    title="MoviePilot用户认证成功",
                    text=f"使用站点：{msg}，如有插件使用异常，请重启MoviePilot。",
                    link=config.site_link,
                )
            )
            # 认证通过后重新初始化插件
            get_plugin_manager().init_config()
            self.init_plugin_jobs()
            self._auth_plugin_routes_pending = True
            register_plugin_api()
            self._auth_plugin_routes_pending = False

        else:
            self._auth_count += 1
            logger.error(f"用户认证失败，{msg}，共失败 {self._auth_count} 次")
            if self._auth_count >= __max_try__:
                logger.error("用户认证失败次数过多，将不再尝试认证！")
