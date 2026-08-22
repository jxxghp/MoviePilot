import asyncio
import gc
import hashlib
import inspect
import multiprocessing
import threading
import time
import traceback
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
from app.runtime.extensions.plugin_manager import PluginManager
from app.db.oper.agenttask import AgentTaskOper
from app.application.database import get_database_governance
from app.application.outbox import dispatch_pending_outbox
from app.application.configuration import (
    SchedulerRuntimeConfig,
    get_configured_system_config,
    get_scheduler_runtime_config,
)
from app.application.image import WallpaperHelper
from app.application.messaging.message import MessageHelper
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.adapters.external.server import MoviePilotServerHelper
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
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
        # 进程启动时只对账一次，配置热重载不得改写仍在执行的任务状态
        self._agent_task_interruptions_reconciled = False
        # 用户认证失败次数
        self._auth_count = 0
        # 用户认证失败消息发送
        self._auth_message = False

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
        self._jobs[job_id] = JobSpec(
            job_id,
            "数据库备份",
            self.database_backup,
            "database",
            recovery=JobRecoveryPolicy.DURABLE_QUEUE,
        ).to_runtime_state()
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

    def init(self) -> None:
        """
        初始化定时服务
        """

        config = get_scheduler_runtime_config()
        # 停止定时服务
        self.stop()

        # 调试模式不启动定时服务
        if config.dev:
            return

        # 对账上个进程未收口的 Agent 任务；进程内重复初始化不会重复改写状态。
        self._reconcile_agent_task_interruptions()

        with lock:
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
                JobSpec("plugin_market_refresh", "插件市场缓存", PluginManager().async_get_online_plugins, "plugin", kwargs={"force": True}),
                JobSpec("subscribe_calendar_cache", "订阅日历缓存", SubscribeChain().cache_calendar, "subscription"),
                JobSpec("full_gc", "主动内存回收", self.full_gc, "runtime"),
                JobSpec("agent_heartbeat", "智能体定时任务", self.agent_heartbeat, "agent"),
                JobSpec("usage_report", "安装版本统计上报", MoviePilotServerHelper.report_usage, "server"),
            ]).runtime_states()

            self._scheduler = BackgroundScheduler(
                timezone=config.timezone,
                executors={"default": ThreadPoolExecutor(config.scheduler_workers)},
            )

            self._register_database_backup_job(config)
            self._jobs["outbox_dispatch"] = JobSpec(
                "outbox_dispatch",
                "恢复待投递副作用",
                dispatch_pending_outbox,
                "outbox",
                recovery=JobRecoveryPolicy.DURABLE_QUEUE,
            ).to_runtime_state()
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
                mediaservers=ServiceConfigHelper.get_mediaserver_configs(),
                default_interval=config.mediaserver_sync_interval,
            )
            for mediaserver_schedule in mediaserver_schedules:
                job_id = mediaserver_schedule["id"]
                self._jobs[job_id] = JobSpec(
                    job_id,
                    mediaserver_schedule["name"],
                    mediaserver_chain.sync,
                    "mediaserver",
                    kwargs={"server": mediaserver_schedule["server"]},
                ).to_runtime_state()
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

    def __prepare_job(self, job_id: str) -> Optional[dict]:
        """
        准备定时任务
        """
        started_at = self._format_time()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if not JobExecutionState.begin(job, started_at):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                record_metric(
                    "scheduler.job.overlap_skip",
                    owner=str(job.get("owner", "unknown")),
                )
                return None
            job["_metric_started_at"] = time.perf_counter()
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
            result = await JobExecutionState.await_result(
                coro,
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
            self._jobs[job_id] = JobSpec(
                job_id,
                task.name,
                self.execute_agent_task,
                "agent",
                recovery=JobRecoveryPolicy.NEXT_SCHEDULE,
                kwargs={"task_id": task_id},
            ).to_runtime_state()
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
            task = AgentTaskOper().get(task_id)
            if task and task.trigger_type == "date" and not task.enabled:
                self.remove_agent_task_job(task_id)

    def init_plugin_jobs(self):
        """
        初始化插件定时服务
        """
        for pid in PluginManager().get_running_plugin_ids():
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
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
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
                self._jobs[job_id] = JobSpec(
                    job_id,
                    workflow.name,
                    WorkflowChain().process,
                    "workflow",
                ).to_runtime_state()
                self._jobs[job_id]["provider_name"] = "工作流"
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
            plugin_manager = PluginManager()
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
                    self._jobs[job_id] = JobSpec(
                        job_id,
                        service["name"],
                        service["func"],
                        f"plugin:{pid}",
                        kwargs=service.get("func_kwargs") or {},
                    ).to_runtime_state()
                    self._jobs[job_id].update(
                        pid=pid,
                        provider_name=plugin_name,
                    )
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
            PluginManager().init_config()
            self.init_plugin_jobs()

        else:
            self._auth_count += 1
            logger.error(f"用户认证失败，{msg}，共失败 {self._auth_count} 次")
            if self._auth_count >= __max_try__:
                logger.error("用户认证失败次数过多，将不再尝试认证！")
