"""调度器静态任务目录与 APScheduler 投影。"""

import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, TypedDict

import pytz  # type: ignore[import-untyped]
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.system.update import system_update_manager
from app.application.configuration import (
    SchedulerRuntimeConfig,
)
from app.application.mediaserver import get_mediaserver_configs
from app.application.outbox import dispatch_pending_outbox
from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import (  # noqa: E402
    JobCatalog,
    JobRecoveryPolicy,
    JobSpec,
)
from app.runtime.scheduling import TimerUtils
from app.scheduler.contract import _SchedulerOwnerBase
from app.scheduler.services import SchedulerServices
from app.schemas.system import MediaServerConf as _SchemaMediaServerConf


class _MediaServerSchedule(TypedDict):
    """媒体服务器同步任务的内部投影。"""

    id: str
    name: str
    server: str
    interval: int


def _subscription_search_job_specs(services: SchedulerServices) -> tuple[JobSpec, ...]:
    """构造订阅搜索、新增搜索与持久队列恢复任务目录。"""
    return (
        JobSpec(
            "subscribe_search", "订阅搜索补全", services.search_subscribe, "subscription", kwargs={"state": "R"}
        ),
        JobSpec(
            "new_subscribe_search",
            "新增订阅搜索",
            services.search_subscribe,
            "subscription",
            kwargs={"state": "N"},
        ),
        JobSpec(
            "subscribe_search_queue",
            "恢复订阅搜索队列",
            services.resume_subscribe_search,
            "subscription",
            recovery=JobRecoveryPolicy.DURABLE_QUEUE,
        ),
    )


class SchedulerCatalogOwner(_SchedulerOwnerBase):
    """调度器静态任务目录与 APScheduler 投影。"""

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
        if interval is None:
            return None
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
    ) -> List[_MediaServerSchedule]:
        """
        构建已启用媒体服务器的独立自动同步任务描述。
        """
        schedules: List[_MediaServerSchedule] = []
        job_ids: set[str] = set()
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

    def _register_subscription_search_queue_job(self, config: SchedulerRuntimeConfig) -> None:
        """注册短周期持久搜索队列恢复任务。"""
        self._scheduler.add_job(
            self.start,
            "interval",
            id="subscribe_search_queue",
            name="恢复订阅搜索队列",
            minutes=1,
            next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(seconds=10),
            kwargs={"job_id": "subscribe_search_queue"},
        )

    def _initialize_catalog(self, config: SchedulerRuntimeConfig) -> None:
        """构建完整任务目录并投影到尚未启动的 APScheduler。"""
        services = self._scheduler_services()
        # 各服务的运行状态
        self._jobs = JobCatalog(
            [
                JobSpec("cookiecloud", "同步CookieCloud站点", services.sync_cookies, "site"),
                JobSpec("mediaserver_sync", "同步媒体服务器", services.sync_mediaserver, "mediaserver"),
                JobSpec("subscribe_tmdb", "订阅元数据更新", services.check_subscribe, "subscription"),
                *_subscription_search_job_specs(services),
                JobSpec("subscribe_refresh", "订阅刷新", services.refresh_subscribe, "subscription"),
                JobSpec("subscribe_follow", "关注的订阅分享", services.follow_subscribe, "subscription"),
                JobSpec(
                    "transfer",
                    "下载文件整理",
                    services.process_transfer,
                    "transfer",
                    recovery=JobRecoveryPolicy.DURABLE_QUEUE,
                ),
                JobSpec(
                    "clear_cache",
                    "缓存清理",
                    self.clear_cache,
                    "runtime",
                    manual=True,
                    recovery=JobRecoveryPolicy.MANUAL_ONLY,
                ),
                JobSpec("data_cleanup", "数据表清理", services.cleanup_data, "database"),
                JobSpec("user_auth", "用户认证检查", self.user_auth, "security"),
                JobSpec("scheduler_job", "公共定时服务", services.run_modules, "module"),
                JobSpec("random_wallpager", "壁纸缓存", services.get_wallpapers, "image"),
                JobSpec("sitedata_refresh", "站点数据刷新", services.refresh_site_data, "site"),
                JobSpec("recommend_refresh", "推荐缓存", services.refresh_recommend, "recommend"),
                JobSpec(
                    "plugin_market_refresh",
                    "插件市场缓存",
                    get_plugin_manager().async_get_online_plugins,
                    "plugin",
                    kwargs={"force": True},
                ),
                JobSpec("subscribe_calendar_cache", "订阅日历缓存", services.cache_subscribe_calendar, "subscription"),
                JobSpec("full_gc", "主动内存回收", self.full_gc, "runtime"),
                JobSpec("agent_heartbeat", "智能体定时任务", self.agent_heartbeat, "agent"),
                JobSpec("usage_report", "安装版本统计上报", MoviePilotServerHelper.report_usage, "server"),
                JobSpec("system_update_check", "检查系统更新", system_update_manager.check, "system"),
            ]
        ).runtime_states()
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
        if config.cookiecloud_interval and str(config.cookiecloud_interval).isdigit():
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
                services.sync_mediaserver,
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
        self._register_subscription_search_queue_job(config)

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

        # 更新检查只缓存 Release 元数据，不会在未授权时下载或重启。
        self._scheduler.add_job(
            self.start,
            "interval",
            id="system_update_check",
            name="检查系统更新",
            hours=6,
            next_run_time=datetime.now(pytz.timezone(config.timezone)) + timedelta(minutes=1),
            kwargs={"job_id": "system_update_check"},
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
