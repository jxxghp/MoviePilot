"""宿主业务定时作业清单。

清单是数据：每条作业声明自己的执行体、运行参数与触发登记，由调度器组合根
统一登记。作业的启用与否在构建清单时按当前配置判定，调度引擎不再认识任何
具体业务。
"""

import hashlib
from datetime import datetime, timedelta
from typing import Callable, List, Optional

import pytz

from app.adapters.external.server import MoviePilotServerHelper
from app.application.image import WallpaperHelper
from app.application.orchestration.mediaserver import MediaServerChain
from app.application.orchestration.recommend import RecommendChain
from app.application.orchestration.scheduler import SchedulerChain
from app.application.orchestration.site import SiteChain
from app.application.orchestration.subscribe import SubscribeChain
from app.application.orchestration.transfer import TransferChain
from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.scheduler import ScheduledJob, ScheduledTrigger
from app.runtime.scheduling import TimerUtils
from app.schemas.system import MediaServerConf
from app.startup.bindings.scheduling import systemjobs

# 订阅 RSS 刷新周期的兜底值与下限，单位分钟
DEFAULT_RSS_INTERVAL = 30
MIN_RSS_INTERVAL = 5
# 站点首页种子刷新模式下每天的随机执行次数
SPIDER_REFRESH_EXECUTIONS = 32


def _now() -> datetime:
    """
    获取系统时区的当前时间。

    :return: 带时区的当前时间
    """
    return datetime.now(pytz.timezone(settings.TZ))


def _mediaserver_sync_interval(
        mediaserver: MediaServerConf,
        default_interval: Optional[int],
) -> Optional[int]:
    """
    获取媒体服务器的有效同步间隔，未设置时回退旧全局配置。

    :param mediaserver: 媒体服务器配置
    :param default_interval: 旧全局同步间隔
    :return: 有效同步间隔小时数，未启用同步时返回 None
    """
    interval = mediaserver.sync_interval
    if interval is None:
        interval = default_interval
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        return None
    return interval if interval > 0 else None


def build_mediaserver_sync_schedules(
        mediaservers: List[MediaServerConf],
        default_interval: Optional[int],
) -> List[dict]:
    """
    构建已启用媒体服务器的独立自动同步任务描述。

    :param mediaservers: 媒体服务器配置列表
    :param default_interval: 旧全局同步间隔
    :return: 任务描述列表，含任务 id、名称、服务器名与间隔
    """
    schedules = []
    job_ids = set()
    for mediaserver in mediaservers:
        if not mediaserver or not mediaserver.enabled or not mediaserver.name:
            continue
        interval = _mediaserver_sync_interval(
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


def _interval(**options) -> ScheduledTrigger:
    """
    构建一条固定周期触发登记。

    :param options: 透传给调度器的周期参数
    :return: 触发登记
    """
    return ScheduledTrigger(trigger="interval", options=options)


def _cookiecloud_triggers() -> tuple:
    """
    构建 CookieCloud 同步的触发登记。

    :return: 触发登记元组，未配置周期时为空
    """
    if not settings.COOKIECLOUD_INTERVAL or not str(settings.COOKIECLOUD_INTERVAL).isdigit():
        return ()
    return (
        _interval(
            minutes=int(settings.COOKIECLOUD_INTERVAL),
            next_run_time=_now() + timedelta(minutes=5),
        ),
    )


def _subscribe_refresh_triggers() -> tuple:
    """
    构建订阅刷新的触发登记。

    站点首页种子模式按随机时刻分散登记多条 cron 触发，RSS 模式登记单条周期触发
    并把越界的周期配置收敛回合法范围。

    :return: 触发登记元组
    """
    if settings.SUBSCRIBE_MODE == "spider":
        return tuple(
            ScheduledTrigger(
                trigger="cron",
                options={"hour": trigger.hour, "minute": trigger.minute},
                suffix=f"|{trigger.hour}:{trigger.minute}",
            )
            for trigger in TimerUtils.random_scheduler(
                num_executions=SPIDER_REFRESH_EXECUTIONS
            )
        )
    if (
            not settings.SUBSCRIBE_RSS_INTERVAL
            or not str(settings.SUBSCRIBE_RSS_INTERVAL).isdigit()
    ):
        settings.SUBSCRIBE_RSS_INTERVAL = DEFAULT_RSS_INTERVAL
    elif int(settings.SUBSCRIBE_RSS_INTERVAL) < MIN_RSS_INTERVAL:
        settings.SUBSCRIBE_RSS_INTERVAL = MIN_RSS_INTERVAL
    return (
        ScheduledTrigger(
            trigger="interval",
            options={"minutes": int(settings.SUBSCRIBE_RSS_INTERVAL)},
            name="RSS订阅刷新",
        ),
    )


def _database_backup_triggers() -> tuple:
    """
    构建数据库备份的触发登记。

    :return: 触发登记元组，未启用备份时为空
    """
    if not settings.DB_BACKUP_ENABLE or not settings.DB_BACKUP_CRON.strip():
        return ()
    return (
        ScheduledTrigger(
            trigger=TimerUtils.build_schedule_trigger(
                trigger_type="cron",
                trigger_value=settings.DB_BACKUP_CRON,
                timezone_name=settings.TZ,
            ),
            replace_existing=True,
        ),
    )


def _mediaserver_jobs(sync: Callable[..., object]) -> List[ScheduledJob]:
    """
    构建每个已启用媒体服务器的独立同步作业。

    :param sync: 媒体服务器同步执行体
    :return: 作业声明列表
    """
    return [
        ScheduledJob(
            id=schedule["id"],
            name=schedule["name"],
            func=sync,
            kwargs={"server": schedule["server"]},
            triggers=(
                _interval(
                    hours=schedule["interval"],
                    next_run_time=_now() + timedelta(minutes=10),
                ),
            ),
        )
        for schedule in build_mediaserver_sync_schedules(
            mediaservers=ServiceConfigHelper.get_mediaserver_configs(),
            default_interval=settings.MEDIASERVER_SYNC_INTERVAL,
        )
    ]


def build_host_jobs(user_auth: Callable[[], None]) -> List[ScheduledJob]:
    """
    构建宿主业务定时作业清单。

    :param user_auth: 用户认证检查执行体，其失败计数由宿主跨配置重载保持
    :return: 作业声明列表
    """
    mediaserver_chain = MediaServerChain()
    jobs = [
        ScheduledJob(
            id="cookiecloud",
            name="同步CookieCloud站点",
            func=SiteChain().sync_cookies,
            triggers=_cookiecloud_triggers(),
        ),
        ScheduledJob(
            id="mediaserver_sync",
            name="同步媒体服务器",
            func=mediaserver_chain.sync,
        ),
        ScheduledJob(
            id="subscribe_tmdb",
            name="订阅元数据更新",
            func=SubscribeChain().check,
            triggers=(_interval(hours=6),),
        ),
        ScheduledJob(
            id="subscribe_search",
            name="订阅搜索补全",
            func=SubscribeChain().search,
            kwargs={"state": "R"},
            triggers=(
                (_interval(hours=settings.SUBSCRIBE_SEARCH_INTERVAL),)
                if settings.SUBSCRIBE_SEARCH
                else ()
            ),
        ),
        ScheduledJob(
            id="new_subscribe_search",
            name="新增订阅搜索",
            func=SubscribeChain().search,
            kwargs={"state": "N"},
            triggers=(_interval(minutes=5),),
        ),
        ScheduledJob(
            id="subscribe_refresh",
            name="订阅刷新",
            func=SubscribeChain().refresh,
            triggers=_subscribe_refresh_triggers(),
        ),
        ScheduledJob(
            id="subscribe_follow",
            name="关注的订阅分享",
            func=SubscribeChain().follow,
            triggers=(_interval(hours=1),),
        ),
        ScheduledJob(
            id="transfer",
            name="下载文件整理",
            func=TransferChain().process,
            triggers=(_interval(minutes=5),),
        ),
        ScheduledJob(
            id="clear_cache",
            name="缓存清理",
            func=systemjobs.clear_cache,
            manual=True,
        ),
        ScheduledJob(
            id="data_cleanup",
            name="数据表清理",
            func=SchedulerChain().cleanup,
            triggers=(
                (
                    ScheduledTrigger(
                        trigger="cron",
                        options={"hour": 3, "minute": 30},
                    ),
                )
                if settings.DATA_CLEANUP_ENABLE
                else ()
            ),
        ),
        ScheduledJob(
            id="user_auth",
            name="用户认证检查",
            func=user_auth,
            triggers=(_interval(minutes=10),),
        ),
        ScheduledJob(
            id="scheduler_job",
            name="公共定时服务",
            func=SchedulerChain().scheduler_job,
            triggers=(_interval(minutes=10),),
        ),
        ScheduledJob(
            id="random_wallpager",
            name="壁纸缓存",
            func=WallpaperHelper().get_wallpapers,
            triggers=(
                _interval(minutes=30, next_run_time=_now() + timedelta(seconds=1)),
            ),
        ),
        ScheduledJob(
            id="sitedata_refresh",
            name="站点数据刷新",
            func=SiteChain().refresh_userdatas,
            triggers=(
                (_interval(minutes=settings.SITEDATA_REFRESH_INTERVAL * 60),)
                if settings.SITEDATA_REFRESH_INTERVAL
                else ()
            ),
        ),
        ScheduledJob(
            id="recommend_refresh",
            name="推荐缓存",
            func=RecommendChain().refresh_recommend,
            triggers=(
                _interval(hours=24, next_run_time=_now() + timedelta(seconds=5)),
            ),
        ),
        ScheduledJob(
            id="plugin_market_refresh",
            name="插件市场缓存",
            func=PluginManager().async_get_online_plugins,
            kwargs={"force": True},
            triggers=(_interval(minutes=30),),
        ),
        ScheduledJob(
            id="subscribe_calendar_cache",
            name="订阅日历缓存",
            func=SubscribeChain().cache_calendar,
            triggers=(
                _interval(hours=6, next_run_time=_now() + timedelta(minutes=2)),
            ),
        ),
        ScheduledJob(
            id="full_gc",
            name="主动内存回收",
            func=systemjobs.full_gc,
            triggers=(
                (_interval(minutes=settings.MEMORY_GC_INTERVAL),)
                if settings.MEMORY_GC_INTERVAL
                else ()
            ),
        ),
        ScheduledJob(
            id="agent_heartbeat",
            name="智能体定时任务",
            func=systemjobs.agent_heartbeat,
            triggers=(
                (_interval(hours=settings.AI_AGENT_JOB_INTERVAL),)
                if settings.AI_AGENT_ENABLE and settings.AI_AGENT_JOB_INTERVAL
                else ()
            ),
        ),
        ScheduledJob(
            id="usage_report",
            name="安装版本统计上报",
            func=MoviePilotServerHelper.report_usage,
            triggers=(
                (_interval(hours=12),)
                if settings.USAGE_STATISTIC_SHARE
                else ()
            ),
        ),
    ]
    # 数据库备份未启用时整条作业都不登记，不给出手动执行入口
    backup_triggers = _database_backup_triggers()
    if backup_triggers:
        jobs.append(
            ScheduledJob(
                id="database_backup",
                name="数据库备份",
                func=systemjobs.database_backup,
                triggers=backup_triggers,
            )
        )
    jobs.extend(_mediaserver_jobs(mediaserver_chain.sync))
    return jobs
