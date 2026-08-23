import asyncio
import concurrent.futures
import gc
import hashlib
import inspect
import multiprocessing
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any, List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress
from app.schemas.system import MediaServerConf as _SchemaMediaServerConf
from app.chain import ChainBase
from app.chain.mediaserver import MediaServerChain
from app.chain.recommend import RecommendChain
from app.chain.site import SiteChain
from app.chain.subscribe import SubscribeChain
from app.chain.transfer import TransferChain
from app.chain.workflow import WorkflowChain
from app.runtime.config import global_vars
from app.runtime.events import Event, eventmanager
from app.db.oper.agenttask import AgentTaskOper
from app.application.database import get_database_governance
from app.application.outbox import dispatch_pending_outbox
from app.application.plugin.runtime import get_plugin_manager
from app.application.configuration import (
    SchedulerRuntimeConfig,
    get_configured_system_config,
    get_scheduler_runtime_config,
)
from app.application.image import WallpaperHelper
from app.application.mediaserver import get_mediaserver_configs
from app.application.messaging.message import MessageHelper
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.adapters.external.server import MoviePilotServerHelper
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.message import MessageType
from app.schemas.workflow import Workflow
from app.schemas.types import EventType, SystemConfigKey
from app.runtime.gc import get_memory_usage
from app.runtime.reload import ConfigReloadMixin
from app.foundation.singleton import SingletonClass
from app.runtime.scheduling import TimerUtils
from app.runtime.correlation import call_with_correlation, get_correlation_id
from app.runtime.observability import record_metric

lock = threading.Lock()
SCHEDULER_PROGRESS_PREFIX = "scheduler"


@dataclass(slots=True)
class _SchedulerHandle:
    """记录调度器提交到事件循环的执行句柄及其 job generation。"""

    job_id: str
    generation: int
    loop: asyncio.AbstractEventLoop
    handle: asyncio.Future[Any] | concurrent.futures.Future[Any]
    completion: asyncio.Future[Any] | concurrent.futures.Future[Any]


# Agent 自主定时任务前缀下沉到 application 门面，此处保留兼容导出。
from app.application.scheduling import (  # noqa: E402
    AGENT_TASK_JOB_PREFIX,
    JobCatalog,
    JobExecutionState,
    JobRecoveryPolicy,
    JobSpec,
)


class SchedulerChain(ChainBase):
    """
    定时任务链，负责执行各类定时任务，包括数据清理等
    """
    # 保留旧常量，插件和维护脚本如有引用无需跟随内部职责迁移。
    DEFAULT_BATCH_SIZE = 500

    def cleanup(
            self,
            batch_size: Optional[int] = None,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """
        按配置保留期执行分批清理。
        """
        return get_database_governance().cleanup(
            batch_size=batch_size,
            progress_callback=progress_callback,
        )


class Scheduler(ConfigReloadMixin, metaclass=SingletonClass):
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
        # 生命周期门禁与事件循环句柄由调度器实例独立持有。
        self._lifecycle_state = "new"
        self._handles: dict[int, _SchedulerHandle] = {}
        self._job_generations: dict[str, int] = {}
        # 运行所有权独立于可热重建的任务定义，避免重载期间同 ID 任务并行执行。
        self._active_job_generations: dict[str, set[int]] = {}
        self._agent_task_reservations: dict[str, int] = {}
        # 进程启动时只对账一次，配置热重载不得改写仍在执行的任务状态
        self._agent_task_interruptions_reconciled = False
        # 用户认证失败次数
        self._auth_count = 0
        # 用户认证失败消息发送
        self._auth_message = False

    async def on_config_changed(self) -> None:
        """
        配置变更后重新初始化定时服务。
        """
        reload_started, scheduler = self._begin_reload()
        if not reload_started:
            return
        await asyncio.to_thread(self._shutdown_scheduler_sync, scheduler)
        with self._lock:
            if self._lifecycle_state != "reloading":
                return
        self.init(_already_stopped=True)

    def get_reload_name(self) -> str:
        """
        获取配置重载日志中的服务名称。
        """
        return "定时服务"

    def _accepting_submissions(self) -> bool:
        """判断调度器是否仍允许提交新的运行实例。"""
        return self._lifecycle_state in {"starting", "running"}

    def _next_job_generation(self, job_id: str) -> int:
        """为同一 job 的下一次注册分配单调 generation。"""
        generation = self._job_generations.get(job_id, 0) + 1
        self._job_generations[job_id] = generation
        return generation

    def _assign_job_generation(self, job_id: str, job: dict[str, Any]) -> None:
        """把注册 generation 写入可变运行时状态。"""
        job["_generation"] = self._next_job_generation(job_id)

    def _is_job_active(self, job_id: str) -> bool:
        """判断任一 generation 的同 ID 任务是否仍在真实执行。"""
        return bool(self._active_job_generations.get(job_id))

    def _release_job_generation(self, job_id: str, generation: int) -> None:
        """在任务真实收尾后释放对应 generation 的运行所有权。"""
        active_generations = self._active_job_generations.get(job_id)
        if not active_generations:
            return
        active_generations.discard(generation)
        if not active_generations:
            self._active_job_generations.pop(job_id, None)

    def _finish_unsubmitted_job(
            self,
            job_id: str,
            job: dict[str, Any],
            generation: int,
            error: Optional[str],
    ) -> None:
        """收尾协程无法提交时同步释放任务状态和运行所有权。"""
        finished_at = self._format_time()
        metric_started_at = None
        with self._lock:
            if generation not in self._active_job_generations.get(job_id, set()):
                return
            current_job = self._jobs.get(job_id)
            if current_job is job and current_job.get("_generation", 0) == generation:
                JobExecutionState.finish(job, finished_at, error)
                metric_started_at = job.pop("_metric_started_at", None)
            self._release_job_generation(job_id, generation)
        if metric_started_at is not None:
            record_metric(
                "scheduler.job.duration",
                time.perf_counter() - metric_started_at,
                owner=str(job.get("owner", "unknown")),
                outcome="success" if error is None else "error",
            )

    def _remove_handle(
            self,
            handle: asyncio.Future[Any] | concurrent.futures.Future[Any],
    ) -> None:
        """执行句柄完成后从 owner registry 移除。"""
        with self._lock:
            self._handles.pop(id(handle), None)

    def _register_handle(
            self,
            job_id: str,
            generation: int,
            loop: asyncio.AbstractEventLoop,
            handle: asyncio.Future[Any] | concurrent.futures.Future[Any],
            completion: asyncio.Future[Any] | concurrent.futures.Future[Any] | None = None,
    ) -> bool:
        """登记调度器拥有的句柄；关闭竞态下拒绝并取消新句柄。"""
        if completion is None:
            completion = handle
        with self._lock:
            if not self._accepts_handle(job_id, generation):
                if isinstance(handle, concurrent.futures.Future):
                    handle.cancel()
                elif loop.is_running():
                    loop.call_soon_threadsafe(handle.cancel)
                else:
                    handle.cancel()
                return False
            self._handles[id(completion)] = _SchedulerHandle(
                job_id=job_id,
                generation=generation,
                loop=loop,
                handle=handle,
                completion=completion,
            )
        completion.add_done_callback(self._remove_handle)
        return True

    def _accepts_handle(self, job_id: str, generation: int) -> bool:
        """判断新句柄是否属于当前运行期或热重载中的既有任务。"""
        if self._accepting_submissions():
            return True
        current_job = self._jobs.get(job_id)
        return bool(
            self._lifecycle_state == "reloading"
            and current_job is not None
            and current_job.get("_generation", 0) == generation
            and current_job.get("running")
        )

    @staticmethod
    def _cancel_handle(handle: _SchedulerHandle) -> None:
        """从句柄所属线程安全地请求取消。"""
        target = handle.handle
        if isinstance(target, concurrent.futures.Future):
            target.cancel()
            return
        if target.done():
            return
        if target.get_loop().is_running():
            target.get_loop().call_soon_threadsafe(target.cancel)
        else:
            target.cancel()

    @staticmethod
    async def _wait_handle(handle: _SchedulerHandle) -> None:
        """等待取消请求到达协程 finally，而不是只等待提交代理变为 cancelled。"""
        target = handle.completion
        if isinstance(target, concurrent.futures.Future):
            await asyncio.shield(asyncio.wrap_future(target))
            return
        if target.get_loop() is asyncio.get_running_loop():
            await asyncio.shield(target)

    async def _await_cancelled_handles(
            self,
            handles: tuple[_SchedulerHandle, ...],
    ) -> None:
        """等待已投递协程结束，关闭总预算由应用生命周期统一控制。"""
        if not handles:
            return
        await asyncio.gather(
            *(self._wait_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    @staticmethod
    def _track_cross_thread_completion(
            coro: Any,
            completion: concurrent.futures.Future[Any],
            started: threading.Event,
    ) -> Any:
        """把跨线程提交代理与协程真实终态分离。"""
        async def _tracked() -> None:
            started.set()
            try:
                result = await coro
            except asyncio.CancelledError:
                if not completion.done():
                    completion.cancel()
            except Exception as err:
                if not completion.done():
                    completion.set_exception(err)
            else:
                if not completion.done():
                    completion.set_result(result)

        return _tracked()

    def _submit_cross_thread(
            self,
            coro: Any,
            *,
            target_loop: asyncio.AbstractEventLoop,
            job_id: str,
            generation: int,
            on_unstarted_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """向主循环提交协程，并以独立完成信号跟踪真实收尾。"""
        completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
        handle: concurrent.futures.Future[Any] = concurrent.futures.Future()
        started = threading.Event()
        tracked = self._track_cross_thread_completion(coro, completion, started)
        task_lock = threading.Lock()
        target_task: asyncio.Task[Any] | None = None

        def complete_target_task(task: asyncio.Task[Any]) -> None:
            if task.cancelled() and not started.is_set():
                if on_unstarted_cancel:
                    on_unstarted_cancel()
                if not completion.done():
                    completion.cancel()
            elif not completion.done():
                error = task.exception()
                if error is None:
                    completion.set_result(None)
                else:
                    completion.set_exception(error)
            if not handle.done():
                handle.set_result(None)

        def start_on_target_loop() -> None:
            nonlocal target_task
            with task_lock:
                if handle.cancelled():
                    tracked.close()
                    coro.close()
                    if on_unstarted_cancel:
                        on_unstarted_cancel()
                    completion.cancel()
                    return
                target_task = target_loop.create_task(tracked)
                target_task.add_done_callback(complete_target_task)

        def cancel_target_task(submitted: concurrent.futures.Future[Any]) -> None:
            if not submitted.cancelled():
                return
            with task_lock:
                task = target_task
            if task is not None and not task.done():
                target_loop.call_soon_threadsafe(task.cancel)

        with self._lock:
            if not self._accepts_handle(job_id, generation):
                tracked.close()
                coro.close()
                return False
            try:
                target_loop.call_soon_threadsafe(start_on_target_loop)
            except RuntimeError:
                tracked.close()
                coro.close()
                return False

            registered = self._register_handle(
                job_id=job_id,
                generation=generation,
                loop=target_loop,
                handle=handle,
                completion=completion,
            )
            handle.add_done_callback(cancel_target_task)
        return registered

    @staticmethod
    def _get_mediaserver_sync_interval(
            mediaserver: _SchemaMediaServerConf,
            default_interval: Optional[int],
    ) -> Optional[int]:
        """
        获取媒体服务器的有效同步间隔，未设置时回退旧全局配置。
        """
        interval = mediaserver.sync_interval
        if interval is None:
            interval = default_interval
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            return None
        return interval if interval > 0 else None

    @classmethod
    def _build_mediaserver_sync_schedules(
            cls,
            mediaservers: List[_SchemaMediaServerConf],
            default_interval: Optional[int],
    ) -> List[dict]:
        """
        构建已启用媒体服务器的独立自动同步任务描述。
        """
        schedules = []
        job_ids = set()
        for mediaserver in mediaservers:
            if not mediaserver or not mediaserver.enabled or not mediaserver.name:
                continue
            interval = cls._get_mediaserver_sync_interval(
                mediaserver=mediaserver,
                default_interval=default_interval,
            )
            if not interval:
                continue
            digest = hashlib.sha256(mediaserver.name.encode("utf-8")).hexdigest()[:12]
            job_id = f"mediaserver_sync_{digest}"
            if job_id in job_ids:
                continue
            job_ids.add(job_id)
            schedules.append(
                {
                    "id": job_id,
                    "name": f"同步媒体服务器 - {mediaserver.name}",
                    "server": mediaserver.name,
                    "interval": interval,
                }
            )
        return schedules

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

    @staticmethod
    def database_backup():
        """按当前宿主策略创建一次定时数据库备份。"""
        return get_database_governance().create_backup()

    def _register_database_backup_job(
            self,
            config: SchedulerRuntimeConfig,
    ) -> None:
        """在共享调度器中按当前配置维护唯一的数据库备份作业。"""
        if not config.db_backup_enable or not config.db_backup_cron.strip():
            return

        job_id = "database_backup"
        job = JobSpec(
            job_id,
            "数据库备份",
            self.database_backup,
            "database",
            recovery=JobRecoveryPolicy.DURABLE_QUEUE,
        ).to_runtime_state()
        self._assign_job_generation(job_id, job)
        self._jobs[job_id] = job
        self._scheduler.add_job(
            self.start,
            trigger=TimerUtils.build_schedule_trigger(
                trigger_type="cron",
                trigger_value=config.db_backup_cron,
                timezone_name=config.timezone,
            ),
            id=job_id,
            name="数据库备份",
            kwargs={"job_id": job_id},
            replace_existing=True,
        )

    def init(self, *, _already_stopped: bool = False) -> None:
        """
        初始化定时服务
        """

        config = get_scheduler_runtime_config()
        # 停止定时服务
        if not _already_stopped:
            self.stop()

        # 调试模式不启动定时服务
        if config.dev:
            with self._lock:
                self._lifecycle_state = "stopped"
            return

        # 对账上个进程未收口的 Agent 任务；进程内重复初始化不会重复改写状态。
        self._reconcile_agent_task_interruptions()

        with lock:
            with self._lock:
                self._event.clear()
                self._lifecycle_state = "starting"
            # 各服务的运行状态
            mediaserver_chain = MediaServerChain()
            self._jobs = JobCatalog([
                JobSpec("cookiecloud", "同步CookieCloud站点", SiteChain().sync_cookies, "site"),
                JobSpec("mediaserver_sync", "同步媒体服务器", mediaserver_chain.sync, "mediaserver"),
                JobSpec("subscribe_tmdb", "订阅元数据更新", SubscribeChain().check, "subscription"),
                JobSpec("subscribe_search", "订阅搜索补全", SubscribeChain().search, "subscription", kwargs={"state": "R"}),
                JobSpec("new_subscribe_search", "新增订阅搜索", SubscribeChain().search, "subscription", kwargs={"state": "N"}),
                JobSpec("subscribe_refresh", "订阅刷新", SubscribeChain().refresh, "subscription"),
                JobSpec("subscribe_follow", "关注的订阅分享", SubscribeChain().follow, "subscription"),
                JobSpec("transfer", "下载文件整理", TransferChain().process, "transfer", recovery=JobRecoveryPolicy.DURABLE_QUEUE),
                JobSpec("clear_cache", "缓存清理", self.clear_cache, "runtime", manual=True, recovery=JobRecoveryPolicy.MANUAL_ONLY),
                JobSpec("data_cleanup", "数据表清理", SchedulerChain().cleanup, "database"),
                JobSpec("user_auth", "用户认证检查", self.user_auth, "security"),
                JobSpec("scheduler_job", "公共定时服务", SchedulerChain().scheduler_job, "module"),
                JobSpec("random_wallpager", "壁纸缓存", WallpaperHelper().get_wallpapers, "image"),
                JobSpec("sitedata_refresh", "站点数据刷新", SiteChain().refresh_userdatas, "site"),
                JobSpec("recommend_refresh", "推荐缓存", RecommendChain().refresh_recommend, "recommend"),
                JobSpec("plugin_market_refresh", "插件市场缓存", get_plugin_manager().async_get_online_plugins, "plugin", kwargs={"force": True}),
                JobSpec("subscribe_calendar_cache", "订阅日历缓存", SubscribeChain().cache_calendar, "subscription"),
                JobSpec("full_gc", "主动内存回收", self.full_gc, "runtime"),
                JobSpec("agent_heartbeat", "智能体定时任务", self.agent_heartbeat, "agent"),
                JobSpec("usage_report", "安装版本统计上报", MoviePilotServerHelper.report_usage, "server"),
            ]).runtime_states()
            for job_id, job in self._jobs.items():
                self._assign_job_generation(job_id, job)

            self._scheduler = BackgroundScheduler(
                timezone=config.timezone,
                executors={"default": ThreadPoolExecutor(config.scheduler_workers)},
            )

            self._register_database_backup_job(config)
            outbox_job = JobSpec(
                "outbox_dispatch",
                "恢复待投递副作用",
                dispatch_pending_outbox,
                "outbox",
                recovery=JobRecoveryPolicy.DURABLE_QUEUE,
            ).to_runtime_state()
            self._assign_job_generation("outbox_dispatch", outbox_job)
            self._jobs["outbox_dispatch"] = outbox_job
            self._scheduler.add_job(
                self.start,
                "interval",
                id="outbox_dispatch",
                name="恢复待投递副作用",
                seconds=30,
                next_run_time=datetime.now(pytz.timezone(config.timezone)),
                kwargs={"job_id": "outbox_dispatch"},
                replace_existing=True,
            )

            # CookieCloud定时同步
            if (
                    config.cookiecloud_interval
                    and str(config.cookiecloud_interval).isdigit()
            ):
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="cookiecloud",
                    name="同步CookieCloud站点",
                    minutes=int(config.cookiecloud_interval),
                    next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(minutes=5),
                    kwargs={"job_id": "cookiecloud"},
                )

            # 按媒体服务器分别注册自动同步任务
            mediaserver_schedules = self._build_mediaserver_sync_schedules(
                mediaservers=get_mediaserver_configs(include_disabled=True),
                default_interval=config.mediaserver_sync_interval,
            )
            for mediaserver_schedule in mediaserver_schedules:
                job_id = mediaserver_schedule["id"]
                job = JobSpec(
                    job_id,
                    mediaserver_schedule["name"],
                    mediaserver_chain.sync,
                    "mediaserver",
                    kwargs={"server": mediaserver_schedule["server"]},
                ).to_runtime_state()
                self._assign_job_generation(job_id, job)
                self._jobs[job_id] = job
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id=job_id,
                    name=mediaserver_schedule["name"],
                    hours=mediaserver_schedule["interval"],
                    next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(minutes=10),
                    kwargs={"job_id": job_id},
                )

            # 新增订阅时搜索（5分钟检查一次）
            self._scheduler.add_job(
                self.start,
                "interval",
                id="new_subscribe_search",
                name="新增订阅搜索",
                minutes=5,
                kwargs={"job_id": "new_subscribe_search"},
            )

            # 检查更新订阅TMDB数据（每隔6小时）
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_tmdb",
                name="订阅元数据更新",
                hours=6,
                kwargs={"job_id": "subscribe_tmdb"},
            )

            # 订阅状态每隔24小时搜索一次
            if config.subscribe_search:
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="subscribe_search",
                    name="订阅搜索补全",
                    hours=config.subscribe_search_interval,
                    kwargs={"job_id": "subscribe_search"},
                )

            if config.subscribe_mode == "spider":
                # 站点首页种子定时刷新模式
                triggers = TimerUtils.random_scheduler(num_executions=32)
                for trigger in triggers:
                    self._scheduler.add_job(
                        self.start,
                        "cron",
                        id=f"subscribe_refresh|{trigger.hour}:{trigger.minute}",
                        name="订阅刷新",
                        hour=trigger.hour,
                        minute=trigger.minute,
                        kwargs={"job_id": "subscribe_refresh"},
                    )
            else:
                # RSS订阅模式
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="subscribe_refresh",
                    name="RSS订阅刷新",
                    minutes=config.subscribe_rss_interval,
                    kwargs={"job_id": "subscribe_refresh"},
                )

            # 关注订阅分享（每1小时）
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_follow",
                name="关注的订阅分享",
                hours=1,
                kwargs={"job_id": "subscribe_follow"},
            )

            # 下载器文件转移（每5分钟）
            self._scheduler.add_job(
                self.start,
                "interval",
                id="transfer",
                name="下载文件整理",
                minutes=5,
                kwargs={"job_id": "transfer"},
            )

            # 后台刷新TMDB壁纸
            self._scheduler.add_job(
                self.start,
                "interval",
                id="random_wallpager",
                name="壁纸缓存",
                minutes=30,
                next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(seconds=1),
                kwargs={"job_id": "random_wallpager"},
            )

            # 公共定时服务
            self._scheduler.add_job(
                self.start,
                "interval",
                id="scheduler_job",
                name="公共定时服务",
                minutes=10,
                kwargs={"job_id": "scheduler_job"},
            )

            # 数据表清理服务，每天凌晨执行一次
            if config.data_cleanup_enable:
                self._scheduler.add_job(
                    self.start,
                    "cron",
                    id="data_cleanup",
                    name="数据表清理",
                    hour=3,
                    minute=30,
                    kwargs={"job_id": "data_cleanup"},
                )

            # 定时检查用户认证，每隔10分钟
            self._scheduler.add_job(
                self.start,
                "interval",
                id="user_auth",
                name="用户认证检查",
                minutes=10,
                kwargs={"job_id": "user_auth"},
            )

            # 站点数据刷新
            if config.sitedata_refresh_interval:
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="sitedata_refresh",
                    name="站点数据刷新",
                    minutes=config.sitedata_refresh_interval * 60,
                    kwargs={"job_id": "sitedata_refresh"},
                )

            # 推荐缓存
            self._scheduler.add_job(
                self.start,
                "interval",
                id="recommend_refresh",
                name="推荐缓存",
                hours=24,
                next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(seconds=5),
                kwargs={"job_id": "recommend_refresh"},
            )

            # 插件市场缓存
            self._scheduler.add_job(
                self.start,
                "interval",
                id="plugin_market_refresh",
                name="插件市场缓存",
                minutes=30,
                kwargs={"job_id": "plugin_market_refresh"},
            )

            # 订阅日历缓存
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_calendar_cache",
                name="订阅日历缓存",
                hours=6,
                next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(minutes=2),
                kwargs={"job_id": "subscribe_calendar_cache"},
            )

            # 主动内存回收
            if config.memory_gc_interval:
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="full_gc",
                    name="主动内存回收",
                    minutes=config.memory_gc_interval,
                    kwargs={"job_id": "full_gc"},
                )

            # 智能体定时任务检查
            if config.ai_agent_enable and config.ai_agent_job_interval:
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="agent_heartbeat",
                    name="智能体定时任务",
                    hours=config.ai_agent_job_interval,
                    kwargs={"job_id": "agent_heartbeat"},
                )

            # 安装版本统计上报
            if config.usage_statistic_share:
                self._scheduler.add_job(
                    self.start,
                    "interval",
                    id="usage_report",
                    name="安装版本统计上报",
                    hours=12,
                    kwargs={"job_id": "usage_report"},
                )

            # 初始化工作流服务
            self.init_workflow_jobs()

            # 恢复 Agent 自主定时任务
            if config.ai_agent_enable:
                self.init_agent_task_jobs()

            # 初始化插件服务
            self.init_plugin_jobs()

            # 启动定时服务
            self._scheduler.start()
            with self._lock:
                self._lifecycle_state = "running"

    def __prepare_job(self, job_id: str) -> Optional[dict]:
        """
        准备定时任务
        """
        started_at = self._format_time()
        with self._lock:
            if not self._accepting_submissions():
                return None
            reservation_owner = self._agent_task_reservations.get(job_id)
            if reservation_owner is not None:
                if reservation_owner != threading.get_ident():
                    return None
                self._agent_task_reservations.pop(job_id, None)
            job = self._jobs.get(job_id)
            if not job:
                return None
            if self._is_job_active(job_id):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                record_metric(
                    "scheduler.job.overlap_skip",
                    owner=str(job.get("owner", "unknown")),
                )
                return None
            if not JobExecutionState.begin(job, started_at):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                record_metric(
                    "scheduler.job.overlap_skip",
                    owner=str(job.get("owner", "unknown")),
                )
                return None
            generation = job.get("_generation", 0)
            self._active_job_generations.setdefault(job_id, set()).add(generation)
            job["_metric_started_at"] = time.perf_counter()
        progress = ProgressHelper(self._get_progress_key(job_id))
        progress.start()
        progress.update(
            value=0,
            text=f"{job.get('name') or job_id} 开始执行 ...",
            data={
                "id": job_id,
                "_generation": job.get("_generation", 0),
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
            job: dict,
            generation: int,
            success: bool = True,
            error: Optional[str] = None,
    ) -> None:
        """
        完成定时任务
        """
        finished_at = self._format_time()
        with self._lock:
            current_job = self._jobs.get(job_id)
            if current_job is not job or current_job.get("_generation", 0) != generation:
                self._release_job_generation(job_id, generation)
                return
            JobExecutionState.finish(job, finished_at, error)
            metric_started_at = job.pop("_metric_started_at", None)
            if metric_started_at is not None:
                record_metric(
                    "scheduler.job.duration",
                    time.perf_counter() - metric_started_at,
                    owner=str(job.get("owner", "unknown")),
                    outcome="success" if success else "error",
                )
        job_name = job.get("name") if job else job_id
        # 收尾可能发生在事件循环上（__run_coro_job），使用异步进度后端避免阻塞
        progress = AsyncProgressHelper(self._get_progress_key(job_id))
        try:
            current_progress = await progress.get() or {}
            progress_value = 100 if success else current_progress.get("value", 0)
            await progress.end(
                text=f"{job_name} {'执行完成' if success else '执行失败'}",
                data={
                    "id": job_id,
                    "_generation": generation,
                    "name": job_name,
                    "provider": job.get("provider_name", "[系统]") if job else None,
                    "status": "success" if success else "failed",
                    "success": success,
                    "finished_at": finished_at,
                    "error": error,
                },
                value=progress_value,
            )
        finally:
            with self._lock:
                self._release_job_generation(job_id, generation)

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
            running = bool(
                job and (self._is_job_active(job_id) or job.get("running"))
            )
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        detail = ProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = dict(detail.get("data") or {})
        progress_generation = data.pop("_generation", None)
        if (
                job
                and progress_generation is not None
                and progress_generation != job.get("_generation", 0)
        ):
            detail = {}
            data = {}
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
            running = bool(
                job and (self._is_job_active(job_id) or job.get("running"))
            )
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        # 异步后端读取，避免在事件循环上阻塞
        detail = await AsyncProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = dict(detail.get("data") or {})
        progress_generation = data.pop("_generation", None)
        if (
                job
                and progress_generation is not None
                and progress_generation != job.get("_generation", 0)
        ):
            detail = {}
            data = {}
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
        generation = job.get("_generation", 0)

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
                "_generation": generation,
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
            }
            if data:
                progress_data.update(data)
            key = self._get_progress_key(job_id)

            async def _update() -> None:
                with self._lock:
                    current_job = self._jobs.get(job_id)
                    if (
                            current_job is not job
                            or current_job.get("_generation", 0) != generation
                    ):
                        return
                # 异步后端更新，避免任务函数在事件循环内调用回调时阻塞
                await AsyncProgressHelper(key).update(
                    value=value,
                    text=text,
                    data=progress_data,
                )

            # 回调可能在事件循环内（async 任务）或线程池中（sync 任务）被调用，
            # 统一经事件循环提交；无运行中循环时同步执行兜底
            self._submit_to_loop(
                _update(),
                job_id=job_id,
                generation=job.get("_generation", 0),
            )

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

    async def __run_coro_job(
            self,
            coro_factory: Callable[[], Any],
            job_id: str,
            job: dict,
            generation: Optional[int] = None,
    ) -> None:
        """
        在当前事件循环内执行协程定时任务并在真实完成后收敛状态。
        """
        generation = job.get("_generation", 0) if generation is None else generation
        success = True
        error = None
        try:
            result = await JobExecutionState.await_result(
                coro_factory(),
                timeout_seconds=job.get("timeout_seconds"),
            )
            error = self.__get_result_error(result)
            success = error is None
        except asyncio.TimeoutError as err:
            success = False
            error = f"任务执行超时（{job.get('timeout_seconds')} 秒）"
            self.__handle_job_error(job_id=job_id, job=job, error=err)
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
            await self.__finish_job(
                job_id=job_id,
                job=job,
                generation=generation,
                success=success,
                error=error,
            )

    def start(self, job_id: str, *args, **kwargs) -> bool:
        """
        启动定时服务
        """

        def __start_coro(
                coro_factory: Callable[[], Any],
                generation: int,
        ) -> tuple[bool, bool]:
            """
            启动协程，返回是否异步收尾以及本次提交是否被接受。
            """
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            target_loop = global_vars.CURRENT_EVENT_LOOP
            target_loop_available = (
                target_loop is not None
                and target_loop.is_running()
                and not target_loop.is_closed()
            )
            if running_loop and (not target_loop_available or running_loop is target_loop):
                started = threading.Event()

                async def run_owned_job() -> None:
                    started.set()
                    await self.__run_coro_job(
                        coro_factory=coro_factory,
                        job_id=job_id,
                        job=job,
                        generation=generation,
                    )

                with self._lock:
                    if not self._accepts_handle(job_id, generation):
                        return False, False
                    handle = running_loop.create_task(run_owned_job())
                    registered = self._register_handle(
                        job_id=job_id,
                        generation=generation,
                        loop=running_loop,
                        handle=handle,
                    )

                    def _finish_cancelled_before_start(
                            submitted: asyncio.Future[Any],
                    ) -> None:
                        if submitted.cancelled() and not started.is_set():
                            self._finish_unsubmitted_job(
                                job_id=job_id,
                                job=job,
                                generation=generation,
                                error="任务未提交",
                            )

                    handle.add_done_callback(_finish_cancelled_before_start)
                    return registered, registered
            if target_loop_available:
                wrapped = self.__run_coro_job(
                    coro_factory=coro_factory,
                    job_id=job_id,
                    job=job,
                    generation=generation,
                )
                submitted = self._submit_cross_thread(
                    wrapped,
                    target_loop=target_loop,
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=lambda: self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        error="任务未提交",
                    ),
                )
                return submitted, submitted
            if self._lifecycle_state in {"stopping", "stopped"}:
                return False, False
            asyncio.run(coro_factory())
            return False, True

        # 获取定时任务
        job = self.__prepare_job(job_id)
        if not job:
            return False
        generation = job.get("_generation", 0)
        success = True
        error = None
        deferred_finish = False
        accepted = True
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
                deferred_finish, accepted = __start_coro(
                    lambda: func(*args, **kwargs), generation
                )
            elif run_in_process:
                # 多进程运行
                p = multiprocessing.Process(
                    target=call_with_correlation,
                    args=(get_correlation_id(), func, args, kwargs),
                )
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
                def finish_without_loop() -> None:
                    self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        error=error if accepted else "任务未提交",
                    )

                # 同步上下文执行异步收尾：优先提交到当前/全局事件循环，无循环时新建循环
                finish_submitted = self._submit_to_loop(
                    self.__finish_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        success=success,
                        error=error,
                    ),
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=finish_without_loop,
                )
                if not finish_submitted:
                    finish_without_loop()
        return accepted

    def _submit_to_loop(
            self,
            coro: Any,
            *,
            job_id: Optional[str] = None,
            generation: int = 0,
            on_unstarted_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        把协程提交到事件循环执行，兼容以下调用环境：
        - 应用主循环可用：统一由主循环拥有任务和关闭顺序
        - 仅调用方循环可用：在当前循环排队为独立任务
        - 无运行中循环（测试/CLI）：新建循环同步执行，确保进度不丢失

        带有 job 标识的句柄由 Scheduler 自己持有，关闭时可以取消并等待。
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        target_loop = global_vars.CURRENT_EVENT_LOOP
        target_loop_available = (
            target_loop is not None
            and target_loop.is_running()
            and not target_loop.is_closed()
        )
        if running_loop and (not target_loop_available or running_loop is target_loop):
            if job_id is not None:
                with self._lock:
                    if not self._accepts_handle(job_id, generation):
                        coro.close()
                        return False
                    handle = running_loop.create_task(coro)
                    registered = self._register_handle(
                        job_id=job_id,
                        generation=generation,
                        loop=running_loop,
                        handle=handle,
                    )
                    if on_unstarted_cancel:
                        handle.add_done_callback(
                            lambda submitted: (
                                on_unstarted_cancel()
                                if submitted.cancelled()
                                else None
                            )
                        )
                    return registered
            else:
                running_loop.create_task(coro)
                return True
        elif target_loop_available:
            if job_id is not None:
                return self._submit_cross_thread(
                    coro,
                    target_loop=target_loop,
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=on_unstarted_cancel,
                )
            else:
                asyncio.run_coroutine_threadsafe(coro, target_loop)
                return True
        elif self._lifecycle_state in {"stopping", "stopped"}:
            coro.close()
            return False
        else:
            asyncio.run(coro)
            return True

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
            if (
                    not self._accepting_submissions()
                    or not job
                    or self._is_job_active(job_id)
                    or job.get("running")
                    or job_id in self._agent_task_reservations
            ):
                return False
            self._agent_task_reservations[job_id] = threading.get_ident()
        try:
            result = self.start(job_id, task_id=task_id, trigger_source="manual")
            return result is not False
        finally:
            with self._lock:
                self._agent_task_reservations.pop(job_id, None)

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
        config = get_scheduler_runtime_config()
        self.remove_agent_task_job(task_id)
        task = AgentTaskOper().get(task_id)
        if (
                not config.ai_agent_enable
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
                timezone_name=config.timezone,
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
            task = await AgentTaskOper().async_get(task_id)
            if task and task.trigger_type == "date" and not task.enabled:
                self.remove_agent_task_job(task_id)

    def init_plugin_jobs(self):
        """
        初始化插件定时服务
        """
        for pid in get_plugin_manager().get_running_plugin_ids():
            self.update_plugin_job(pid)

    @eventmanager.register(EventType.PluginReload)
    def on_plugin_reload(self, event: Event) -> None:
        """插件重载后按当前实例重新注册全部定时服务"""
        plugin_id = event.event_data.get("plugin_id")
        if not plugin_id:
            return
        self.update_plugin_job(plugin_id)

    def init_workflow_jobs(self):
        """
        初始化工作流定时服务
        """
        for workflow in WorkflowChain().get_timer_workflows() or []:
            self.update_workflow_job(workflow)

    def remove_workflow_job(self, workflow: Workflow):
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
                SchedulerChain().messagehelper.put(
                    title=f"工作流 {workflow.name} 服务移除失败",
                    message=str(e),
                    role="system",
                )

    def remove_plugin_job(self, pid: str, job_id: Optional[str] = None):
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
                    (job_id, service)
                    for job_id, service in self._jobs.items()
                    if service.get("pid") == pid
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
                        logger.info(
                            f"移除插件服务({plugin_name})：{service.get('name')}"
                        )  # noqa
                except Exception as e:
                    logger.error(f"移除插件服务失败：{str(e)} - {job_id}: {service}")
                    SchedulerChain().messagehelper.put(
                        title=f"插件 {plugin_name} 服务移除失败",
                        message=str(e),
                        role="system",
                    )

    def update_workflow_job(self, workflow: Workflow):
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
                    WorkflowChain().process,
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
                SchedulerChain().messagehelper.put(
                    title=f"工作流 {workflow.name} 服务注册失败",
                    message=str(e),
                    role="system",
                )

    def update_plugin_job(self, pid: str):
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
                logger.error(
                    f"运行插件 {pid} 服务失败：{str(e)} - {traceback.format_exc()}"
                )
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
                    logger.info(
                        f"注册插件{plugin_name}服务：{service['name']} - {service['trigger']}"
                    )
                except Exception as e:
                    logger.error(f"注册插件{plugin_name}服务失败：{str(e)} - {service}")
                    SchedulerChain().messagehelper.put(
                        title=f"插件 {plugin_name} 服务注册失败",
                        message=str(e),
                        role="system",
                    )

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
                if (
                        (self._is_job_active(job_id) or service.get("running"))
                        and name
                        and provider_name
                ):
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
                status = (
                    "正在运行"
                    if self._is_job_active(job_id) or service.get("running")
                    else "等待"
                )
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
                        status=(
                            "正在运行" if self._is_job_active(job_id) else "等待"
                        ),
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            return schedulers

    def _begin_stop(self) -> tuple[Any, tuple[_SchedulerHandle, ...]]:
        """关闭提交入口并摘出当前调度器与其拥有的异步句柄。"""
        with self._lock:
            self._lifecycle_state = "stopping"
            self._event.set()
            scheduler = self._scheduler
            self._scheduler = None
            self._agent_task_reservations.clear()
            handles = tuple(self._handles.values())
        if scheduler:
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error("移除定时任务失败：%s", err)
        return scheduler, handles

    def _begin_reload(self) -> tuple[bool, Any]:
        """停止旧计划的提交入口，保留已开始任务直到其自然完成。"""
        with self._lock:
            if (
                    global_vars.is_system_stopped
                    or self._lifecycle_state in {"stopping", "reloading"}
            ):
                return False, None
            self._lifecycle_state = "reloading"
            self._event.set()
            scheduler = self._scheduler
            self._scheduler = None
            self._agent_task_reservations.clear()
        if scheduler:
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error("移除定时任务失败：%s", err)
        return True, scheduler

    @staticmethod
    def _shutdown_scheduler_sync(scheduler: Any) -> None:
        """等待 APScheduler 自有线程池停止。"""
        if scheduler and scheduler.running:
            scheduler.shutdown()

    def stop(self) -> None:
        """
        关闭定时服务的同步兼容入口。

        应用生命周期使用 ``stop_async``，以便等待事件循环中的协程句柄；同步
        调用方仍可请求取消并等待 APScheduler 自有线程池收口。
        """
        with lock:
            try:
                scheduler, handles = self._begin_stop()
                for handle in handles:
                    self._cancel_handle(handle)
                self._shutdown_scheduler_sync(scheduler)
                with self._lock:
                    self._lifecycle_state = "stopped"
                logger.info("定时任务停止完成")
            except Exception as err:
                logger.error(f"停止定时任务失败：{err} - {traceback.format_exc()}")

    async def stop_async(self) -> None:
        """关闭调度器并等待已投递协程收口。"""
        scheduler, handles = self._begin_stop()
        for handle in handles:
            self._cancel_handle(handle)
        await asyncio.to_thread(self._shutdown_scheduler_sync, scheduler)
        await self._await_cancelled_handles(handles)
        with self._lock:
            self._lifecycle_state = "stopped"
        logger.info("定时任务停止完成")

    @staticmethod
    def clear_cache():
        """
        清理缓存
        """
        SchedulerChain().clear_cache()

    @staticmethod
    def full_gc():
        """
        主动内存回收
        """
        memory_before = get_memory_usage()
        collected = gc.collect()
        memory_after = get_memory_usage()
        memory_freed = memory_before - memory_after
        logger.info(
            f"主动内存回收完成，回收对象数: {collected}，释放内存: {memory_freed:.2f} MB"
        )

    @staticmethod
    async def agent_heartbeat():
        """
        智能体心跳唤醒：检查并执行待处理的定时任务
        """
        from app.agent.runtime_loader import get_running_agent_manager

        manager = get_running_agent_manager()
        if manager is None:
            logger.debug("智能助手服务未运行，跳过心跳任务")
            return
        await manager.heartbeat_check_jobs()

    def user_auth(self):
        """
        用户认证检查
        """
        config = get_scheduler_runtime_config()
        if SitesHelper().auth_level >= 2:
            return
        # 最大重试次数
        __max_try__ = 30
        if self._auth_count > __max_try__:
            if not self._auth_message:
                SchedulerChain().messagehelper.put(
                    title=f"用户认证失败",
                    message="用户认证失败次数过多，将不再尝试认证！",
                    role="system",
                )
                self._auth_message = True
            return
        logger.info("用户未认证，正在尝试认证...")
        auth_conf = get_configured_system_config().get(
            SystemConfigKey.UserSiteAuthParams
        )
        if auth_conf:
            status, msg = SitesHelper().check_user(**auth_conf)
        else:
            status, msg = SitesHelper().check_user()
        if status:
            self._auth_count = 0
            logger.info(f"{msg} 用户认证成功")
            SchedulerChain().post_message(
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

        else:
            self._auth_count += 1
            logger.error(f"用户认证失败，{msg}，共失败 {self._auth_count} 次")
            if self._auth_count >= __max_try__:
                logger.error("用户认证失败次数过多，将不再尝试认证！")
