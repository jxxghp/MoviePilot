import asyncio
import inspect
import multiprocessing
import threading
import traceback
from datetime import datetime
from typing import Callable, Optional, Any, List

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress
from app.runtime.config import settings, global_vars
from app.application.messaging.message import MessageHelper
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey
from app.runtime.reload import ConfigReloadMixin
from app.runtime.scheduler import ScheduledJob
from app.runtime.scheduling import TimerUtils
from app.foundation.singleton import SingletonClass
from app.startup.scheduling.manifest import build_host_jobs
from app.startup.scheduling.systemjobs import UserAuthChecker
from app.scheduler.agent_tasks import AgentTaskScheduling
from app.scheduler.plugins import PluginScheduling
from app.scheduler.workflows import WorkflowScheduling

lock = threading.Lock()
SCHEDULER_PROGRESS_PREFIX = "scheduler"


class Scheduler(
    AgentTaskScheduling,
    WorkflowScheduling,
    PluginScheduling,
    ConfigReloadMixin,
    metaclass=SingletonClass,
):
    """
    定时任务管理
    """

    CONFIG_WATCH = {
        "DEV",
        "COOKIECLOUD_INTERVAL",
        "MEDIASERVER_SYNC_INTERVAL",
        SystemConfigKey.MediaServers.value,
        "SUBSCRIBE_SEARCH",
        "SUBSCRIBE_SEARCH_INTERVAL",
        "SUBSCRIBE_MODE",
        "SUBSCRIBE_RSS_INTERVAL",
        "SITEDATA_REFRESH_INTERVAL",
        "AI_AGENT_ENABLE",
        "AI_AGENT_JOB_INTERVAL",
        "DATA_CLEANUP_ENABLE",
        "DATA_CLEANUP_MESSAGE_DAYS",
        "DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS",
        "DATA_CLEANUP_SITE_USERDATA_DAYS",
        "DATA_CLEANUP_TRANSFER_HISTORY_DAYS",
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "USAGE_STATISTIC_SHARE",
    }

    def __init__(self):
        """创建调度器状态；后台任务由应用生命周期显式启动。"""
        # 定时服务
        self._scheduler = None
        # 退出事件
        self._event = threading.Event()
        # 锁
        self._lock = threading.RLock()
        # 各服务的运行状态
        self._jobs = {}
        # 进程启动时只对账一次，配置热重载不得改写仍在执行的任务状态
        self._agent_task_interruptions_reconciled = False
        # 认证失败计数跨配置重载保持，认证检查作业与调度器实例同生命周期
        self._user_auth = UserAuthChecker(on_authenticated=self.init_plugin_jobs)

    def on_config_changed(self) -> None:
        """
        配置变更后重新初始化定时服务。
        """
        self.init()

    def get_reload_name(self) -> str:
        """
        获取配置重载日志中的服务名称。
        """
        return "定时服务"

    @staticmethod
    def _get_progress_key(job_id: str) -> str:
        """
        获取定时服务进度缓存键。
        """
        return f"{SCHEDULER_PROGRESS_PREFIX}:{job_id}"

    @staticmethod
    def _format_time(value: Optional[datetime] = None) -> str:
        """
        格式化进度事件时间。
        """
        return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    def init(self) -> None:
        """
        初始化定时服务
        """

        # 停止定时服务
        self.stop()

        # 调试模式不启动定时服务
        if settings.DEV:
            return

        # 对账上个进程未收口的 Agent 任务；进程内重复初始化不会重复改写状态。
        self._reconcile_agent_task_interruptions()

        with lock:
            self._jobs = {}
            self._scheduler = BackgroundScheduler(
                timezone=settings.TZ,
                executors={"default": ThreadPoolExecutor(settings.CONF.scheduler)},
            )

            # 宿主业务作业按清单登记
            for job in build_host_jobs(user_auth=self._user_auth.check):
                self._register_job(job)

            # 初始化工作流服务
            self.init_workflow_jobs()

            # 恢复 Agent 自主定时任务
            if settings.AI_AGENT_ENABLE:
                self.init_agent_task_jobs()

            # 初始化插件服务
            self.init_plugin_jobs()

            # 启动定时服务
            self._scheduler.start()

    def _register_job(self, job: ScheduledJob) -> None:
        """
        登记一条定时作业的运行状态与全部调度器触发。

        :param job: 作业声明
        """
        state = {
            "name": job.name,
            "func": job.func,
            "running": False,
        }
        if job.kwargs:
            state["kwargs"] = job.kwargs
        if job.provider_name:
            state["provider_name"] = job.provider_name
        if job.manual:
            state["manual"] = True
        self._jobs[job.id] = state
        for trigger in job.triggers:
            self._scheduler.add_job(
                self.start,
                trigger=trigger.trigger,
                id=f"{job.id}{trigger.suffix}",
                name=trigger.name or job.name,
                kwargs={"job_id": job.id},
                **({"replace_existing": True} if trigger.replace_existing else {}),
                **trigger.options,
            )

    def __prepare_job(self, job_id: str) -> Optional[dict]:
        """
        准备定时任务
        """
        started_at = self._format_time()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.get("running"):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                return None
            self._jobs[job_id]["running"] = True
            self._jobs[job_id]["last_started_at"] = started_at
            self._jobs[job_id]["last_finished_at"] = None
            self._jobs[job_id]["last_error"] = None
        progress = ProgressHelper(self._get_progress_key(job_id))
        progress.start()
        progress.update(
            value=0,
            text=f"{job.get('name') or job_id} 开始执行 ...",
            data={
                "id": job_id,
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
            },
        )
        return job

    async def __finish_job(
            self,
            job_id: str,
            success: bool = True,
            error: Optional[str] = None,
    ) -> None:
        """
        完成定时任务
        """
        finished_at = self._format_time()
        job = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["running"] = False
                job["last_finished_at"] = finished_at
                job["last_error"] = error
        job_name = job.get("name") if job else job_id
        # 收尾可能发生在事件循环上（__run_coro_job），使用异步进度后端避免阻塞
        progress = AsyncProgressHelper(self._get_progress_key(job_id))
        current_progress = await progress.get() or {}
        progress_value = 100 if success else current_progress.get("value", 0)
        await progress.end(
            text=f"{job_name} {'执行完成' if success else '执行失败'}",
            data={
                "id": job_id,
                "name": job_name,
                "provider": job.get("provider_name", "[系统]") if job else None,
                "status": "success" if success else "failed",
                "success": success,
                "finished_at": finished_at,
                "error": error,
            },
            value=progress_value,
        )

    def get_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度。
        """
        if not job_id:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            job_name = job.get("name") if job else job_id
            provider_name = job.get("provider_name", "[系统]") if job else None
            running = bool(job.get("running")) if job else False
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        detail = ProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = detail.get("data") or {}
        value = detail.get("value", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        return _SchemaScheduleProgress(
            id=job_id,
            name=data.get("name") or job_name,
            provider=data.get("provider") or provider_name,
            enable=bool(detail.get("enable", running)),
            value=max(min(value, 100), 0),
            text=detail.get("text"),
            status=data.get("status") or ("running" if running else "waiting"),
            success=data.get("success"),
            started_at=data.get("started_at") or last_started_at,
            finished_at=data.get("finished_at") or last_finished_at,
            error=data.get("error") or last_error,
            data=data,
        )

    async def aget_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度（异步版本，供事件循环上的端点使用）。
        """
        if not job_id:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            job_name = job.get("name") if job else job_id
            provider_name = job.get("provider_name", "[系统]") if job else None
            running = bool(job.get("running")) if job else False
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        # 异步后端读取，避免在事件循环上阻塞
        detail = await AsyncProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = detail.get("data") or {}
        value = detail.get("value", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        return _SchemaScheduleProgress(
            id=job_id,
            name=data.get("name") or job_name,
            provider=data.get("provider") or provider_name,
            enable=bool(detail.get("enable", running)),
            value=max(min(value, 100), 0),
            text=detail.get("text"),
            status=data.get("status") or ("running" if running else "waiting"),
            success=data.get("success"),
            started_at=data.get("started_at") or last_started_at,
            finished_at=data.get("finished_at") or last_finished_at,
            error=data.get("error") or last_error,
            data=data,
        )

    @staticmethod
    def __handle_job_error(job_id: str, job: dict, error: Exception) -> None:
        """
        记录定时任务执行异常并发送系统错误事件。
        """
        logger.error(
            f"定时任务 {job.get('name')} 执行失败：{str(error)} - {traceback.format_exc()}"
        )
        MessageHelper().put(
            title=f"{job.get('name')} 执行失败", message=str(error), role="system"
        )
        eventmanager.send_event(
            EventType.SystemError,
            {
                "type": "scheduler",
                "scheduler_id": job_id,
                "scheduler_name": job.get("name"),
                "error": str(error),
                "traceback": traceback.format_exc(),
            },
        )

    def __build_progress_callback(self, job_id: str, job: dict) -> Callable[..., None]:
        """
        构建传递给定时任务内部的进度更新回调。
        """

        def update_progress(
                value: Optional[float] = None,
                text: Optional[str] = None,
                data: Optional[dict] = None,
        ) -> None:
            """
            更新当前定时任务进度。
            """
            progress_data = {
                "id": job_id,
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
            }
            if data:
                progress_data.update(data)
            key = self._get_progress_key(job_id)

            async def _update() -> None:
                # 异步后端更新，避免任务函数在事件循环内调用回调时阻塞
                await AsyncProgressHelper(key).update(
                    value=value,
                    text=text,
                    data=progress_data,
                )

            # 回调可能在事件循环内（async 任务）或线程池中（sync 任务）被调用，
            # 统一经事件循环提交；无运行中循环时同步执行兜底
            self._submit_to_loop(_update())

        return update_progress

    @staticmethod
    def __supports_progress_callback(func: Callable[..., Any]) -> bool:
        """
        判断定时任务函数是否显式支持进度回调参数。
        """
        try:
            parameters = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        return "progress_callback" in parameters

    @staticmethod
    def __get_result_error(result: Any) -> Optional[str]:
        """
        从定时任务标准失败返回值中提取错误信息。
        """
        if (
                isinstance(result, tuple)
                and result
                and isinstance(result[0], bool)
                and result[0] is False
        ):
            return str(result[1]) if len(result) > 1 and result[1] else "定时任务返回失败"
        return None

    async def __run_coro_job(self, coro, job_id: str, job: dict) -> None:
        """
        在当前事件循环内执行协程定时任务并在真实完成后收敛状态。
        """
        success = True
        error = None
        try:
            result = await coro
            error = self.__get_result_error(result)
            success = error is None
        except asyncio.CancelledError:
            success = False
            error = "任务已取消"
            raise
        except Exception as err:
            success = False
            error = str(err)
            self.__handle_job_error(job_id=job_id, job=job, error=err)
        finally:
            # 协程收尾在事件循环上完成，同步路径（线程池/调用线程）提交到事件循环执行
            await self.__finish_job(job_id=job_id, success=success, error=error)

    def start(self, job_id: str, *args, **kwargs) -> None:
        """
        启动定时服务
        """

        def __start_coro(coro) -> bool:
            """
            启动协程，返回是否由异步回调自行收敛任务状态。
            """
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            target_loop = global_vars.loop
            if running_loop:
                asyncio.create_task(self.__run_coro_job(coro=coro, job_id=job_id, job=job))
                return True
            if target_loop and target_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.__run_coro_job(coro=coro, job_id=job_id, job=job),
                    target_loop,
                )
                return True
            asyncio.run(coro)
            return False

        # 获取定时任务
        job = self.__prepare_job(job_id)
        if not job:
            return
        success = True
        error = None
        deferred_finish = False
        # 开始运行
        try:
            if not kwargs:
                kwargs = dict(job.get("kwargs") or {})
            func = job.get("func")
            if not func:
                return
            if self.__supports_progress_callback(func) and "progress_callback" not in kwargs:
                kwargs["progress_callback"] = self.__build_progress_callback(
                    job_id=job_id, job=job
                )
            # 是否多进程运行
            run_in_process = job.get("run_in_process", False)
            if inspect.iscoroutinefunction(func):
                # 协程函数
                deferred_finish = __start_coro(func(*args, **kwargs))
            elif run_in_process:
                # 多进程运行
                p = multiprocessing.Process(target=func, args=args, kwargs=kwargs)
                p.start()
                p.join()
            else:
                # 普通函数
                result = func(*args, **kwargs)
                error = self.__get_result_error(result)
                success = error is None
        except Exception as e:
            success = False
            error = str(e)
            self.__handle_job_error(job_id=job_id, job=job, error=e)
        finally:
            if not deferred_finish:
                # 同步上下文执行异步收尾：优先提交到当前/全局事件循环，无循环时新建循环
                self._submit_to_loop(self.__finish_job(
                    job_id=job_id, success=success, error=error
                ))

    @staticmethod
    def _submit_to_loop(coro: Any) -> None:
        """
        把协程提交到事件循环执行，兼容以下调用环境：
        - 已在事件循环内（async 任务内部）：排队为独立任务，避免阻塞
        - 外部线程且全局循环在运行：跨线程提交，非阻塞
        - 无运行中循环（测试/CLI）：新建循环同步执行，确保进度不丢失
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop:
            asyncio.create_task(coro)
        elif global_vars.loop and global_vars.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, global_vars.loop)
        else:
            asyncio.run(coro)

    def list(self) -> List[_SchemaScheduleInfo]:
        """
        当前所有任务
        """
        if not self._scheduler:
            return []
        with self._lock:
            # 返回计时任务
            schedulers = []
            # 去重
            added = []
            # 避免_scheduler.shutdown()处于阻塞状态导致的死锁
            if not self._scheduler or not self._scheduler.running:
                return []
            jobs = self._scheduler.get_jobs()
            # 按照下次运行时间排序
            jobs.sort(key=lambda x: x.next_run_time)
            # 将正在运行的任务提取出来 (保障一次性任务正常显示)
            for job_id, service in self._jobs.items():
                name = service.get("name")
                provider_name = service.get("provider_name")
                if service.get("running") and name and provider_name:
                    if job_id not in added:
                        added.append(job_id)
                    progress = self.get_progress(job_id)
                    schedulers.append(
                        _SchemaScheduleInfo(
                            id=job_id,
                            name=name,
                            provider=provider_name,
                            status="正在运行",
                            progress=progress.value if progress else 0,
                            progress_text=progress.text if progress else None,
                            progress_enable=progress.enable if progress else False,
                            progress_detail=progress,
                        )
                    )
            # 获取其他待执行任务
            for job in jobs:
                job_id = job.id.split("|")[0]
                if job_id not in added:
                    added.append(job_id)
                else:
                    continue
                service = self._jobs.get(job_id)
                if not service:
                    continue
                # 任务状态
                status = "正在运行" if service.get("running") else "等待"
                # 下次运行时间
                next_run = TimerUtils.time_difference(job.next_run_time)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=job.name,
                        provider=service.get("provider_name", "[系统]"),
                        status=status,
                        next_run=next_run,
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            # 仅手动执行的任务（未注册到调度器）
            for job_id, service in self._jobs.items():
                if not service.get("manual"):
                    continue
                if job_id in added:
                    continue
                added.append(job_id)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=service.get("name"),
                        provider=service.get("provider_name", "[系统]"),
                        status="等待",
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            return schedulers

    def stop(self):
        """
        关闭定时服务
        """
        with lock:
            try:
                if self._scheduler:
                    logger.info("正在停止定时任务...")
                    self._event.set()
                    self._scheduler.remove_all_jobs()
                    if self._scheduler.running:
                        self._scheduler.shutdown()
                    self._scheduler = None
                    logger.info("定时任务停止完成")
            except Exception as e:
                logger.error(f"停止定时任务失败：：{str(e)} - {traceback.format_exc()}")
