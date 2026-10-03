"""插件一次性任务：在宿主调度器上追加、取消与回收插件的延迟单次执行。"""

from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.date import DateTrigger

from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import JobSpec
from app.runtime.log import logger, wrap_for_plugin_instance
from app.scheduler.contract import _SchedulerOwnerBase

# 插件一次性任务在运行时 job ID 中的命名段，避免与 get_service() 声明的服务 ID 冲突。
PLUGIN_ONCE_JOB_SEGMENT = "once"


class SchedulerPluginOnceJobOwner(_SchedulerOwnerBase):
    """插件一次性任务的注册、取消和随插件实例的清理。"""

    @staticmethod
    def _get_plugin_once_job_id(pid: str, job_id: str) -> str:
        """生成插件一次性任务的运行时 job ID。"""
        return f"{pid}_{PLUGIN_ONCE_JOB_SEGMENT}_{job_id}"

    def add_plugin_once_job(
        self,
        pid: str,
        job_id: str,
        func: Callable[..., Any],
        name: str,
        delay_seconds: float = 0,
        func_kwargs: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        为插件追加一个延迟执行一次的任务，不影响该插件 get_service() 声明的周期服务。

        任务复用宿主线程池、执行状态、进度和插件实例日志上下文；同一插件内同 ID
        重复追加时替换尚未执行的旧任务。插件卸载时随 remove_plugin_job(pid) 清除；
        插件重载换成新实例后，旧实例追加的任务在服务重建时丢弃，不会回调已停止的实例。
        :param pid: 插件 ID
        :param job_id: 插件内唯一的任务 ID，不得包含 "|"
        :param func: 执行函数，通常是插件实例的绑定方法
        :param name: 仪表盘显示的任务名称
        :param delay_seconds: 延迟秒数，0 表示尽快执行
        :param func_kwargs: 传给执行函数的关键字参数
        :return: 调度器运行中且注册成功时返回 True
        """
        if not pid or not job_id or "|" in job_id:
            logger.error(f"插件一次性任务 ID 无效：{pid} - {job_id}")
            return False
        runtime_id = self._get_plugin_once_job_id(pid, job_id)
        with self._lock:
            if not self._scheduler or not self._accepting_submissions():
                logger.warning(f"调度器未运行，无法注册插件一次性任务：{pid} - {name}")
                return False
            self._prune_plugin_once_jobs(pid)
            self._remove_scheduled_job(runtime_id)
            plugin_manager = get_plugin_manager()
            job = JobSpec(
                runtime_id,
                name,
                wrap_for_plugin_instance(func, pid),
                f"plugin:{pid}",
                kwargs=func_kwargs or {},
            ).to_runtime_state()
            self._assign_job_generation(runtime_id, job)
            job.update(
                pid=pid,
                # 插件未运行时读不到名称，用插件 ID 兜底，避免仪表盘和日志显示 None
                provider_name=plugin_manager.get_plugin_attr(pid, "plugin_name") or pid,
                once=True,
                # func 是插件传入的任意可调用对象，只有绑定方法才带 __self__；
                # 记录注册实例，供服务重建时丢弃已被替换实例的遗留任务。
                _plugin_instance=getattr(func, "__self__", None),
            )
            self._jobs[runtime_id] = job
            run_date = datetime.now(self._scheduler.timezone) + timedelta(seconds=max(delay_seconds, 0))
            try:
                self._scheduler.add_job(
                    self.start,
                    DateTrigger(run_date=run_date),
                    id=runtime_id,
                    name=name,
                    kwargs={"job_id": runtime_id},
                    # 调度线程繁忙导致的延迟不应让一次性任务被静默丢弃。
                    misfire_grace_time=None,
                    replace_existing=True,
                )
            except Exception as e:
                self._jobs.pop(runtime_id, None)
                logger.error(f"注册插件一次性任务失败：{pid} - {name} - {str(e)}")
                return False
        logger.info(f"注册插件一次性任务：{job['provider_name']} - {name}，{delay_seconds} 秒后执行")
        return True

    def remove_plugin_once_job(self, pid: str, job_id: str) -> None:
        """取消插件尚未执行的一次性任务；已在运行的任务不会被中断。"""
        self.remove_plugin_job(pid, self._get_plugin_once_job_id(pid, job_id))

    def _remove_scheduled_job(self, runtime_id: str) -> None:
        """从 APScheduler 移除指定 job，不存在时忽略。"""
        try:
            self._scheduler.remove_job(runtime_id)
        except JobLookupError:
            pass

    def _is_plugin_once_job_pending(self, runtime_id: str, job: dict[str, Any]) -> bool:
        """判断一次性任务是否仍待执行或正在执行；已执行完的状态可以回收。"""
        return (
            self._scheduler.get_job(runtime_id) is not None
            or self._is_job_active(runtime_id)
            or bool(job.get("running"))
        )

    def _prune_plugin_once_jobs(self, pid: str) -> None:
        """回收插件已执行完的一次性任务状态，避免 _jobs 随不同任务 ID 累积。"""
        for runtime_id, job in list(self._jobs.items()):
            if job.get("once") and job.get("pid") == pid and not self._is_plugin_once_job_pending(runtime_id, job):
                self._jobs.pop(runtime_id, None)

    def _detach_live_plugin_once_jobs(self, pid: str) -> dict[str, dict[str, Any]]:
        """
        取出服务重建时应保留的一次性任务。

        保存配置时宿主先调用 init_plugin 再重建服务，init_plugin 里刚追加的任务必须保留；
        注册实例已被重载替换或插件已停止运行时，遗留任务随其余服务一起移除。
        """
        once_jobs = [
            (runtime_id, job) for runtime_id, job in self._jobs.items() if job.get("once") and job.get("pid") == pid
        ]
        if not once_jobs:
            return {}
        current = get_plugin_manager().running_plugins.get(pid)
        kept: dict[str, dict[str, Any]] = {}
        for runtime_id, job in once_jobs:
            instance = job.get("_plugin_instance")
            if (instance is None or instance is current) and self._is_plugin_once_job_pending(runtime_id, job):
                kept[runtime_id] = self._jobs.pop(runtime_id)
        return kept
