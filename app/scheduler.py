import logging
import threading
from datetime import datetime, timedelta
from typing import List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app import schemas
from app.chain import ChainBase
from app.chain.cookiecloud import CookieCloudChain
from app.chain.mediaserver import MediaServerChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.timer import TimerUtils

# 获取 apscheduler 的日志记录器
scheduler_logger = logging.getLogger('apscheduler')

# 设置日志级别为 WARNING
scheduler_logger.setLevel(logging.WARNING)


class SchedulerChain(ChainBase):
    pass


class Scheduler(metaclass=Singleton):
    """
    定时任务管理
    """
    # 定时服务
    _scheduler = BackgroundScheduler(timezone=settings.TZ,
                                     executors={
                                         'default': ThreadPoolExecutor(20)
                                     })
    # 退出事件
    _event = threading.Event()

    def __init__(self):

        def clear_cache():
            """
            清理缓存
            """
            TorrentsChain().clear_cache()
            SchedulerChain().clear_cache()

        # 各服务的运行状态
        self._jobs = {
            "cookiecloud": {
                "func": CookieCloudChain().process,
                "running": False,
            },
            "mediaserver_sync": {
                "func": MediaServerChain().sync,
                "running": False,
            },
            "subscribe_tmdb": {
                "func": SubscribeChain().check,
                "running": False,
            },
            "subscribe_search": {
                "func": SubscribeChain().search,
                "running": False,
                "kwargs": {
                    "state": "R"
                }
            },
            "subscribe_refresh": {
                "func": SubscribeChain().refresh,
                "running": False,
            },
            "transfer": {
                "func": TransferChain().process,
                "running": False,
            },
            "clear_cache": {
                "func": clear_cache,
                "running": False,
            }
        }

        # 调试模式不启动定时服务
        if settings.DEV:
            return

        # CookieCloud定时同步
        if settings.COOKIECLOUD_INTERVAL:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="cookiecloud",
                name="同步CookieCloud站点",
                minutes=settings.COOKIECLOUD_INTERVAL,
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=1),
                kwargs={
                    'job_id': 'cookiecloud'
                }
            )

        # 媒体服务器同步
        if settings.MEDIASERVER_SYNC_INTERVAL:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="mediaserver_sync",
                name="同步媒体服务器",
                hours=settings.MEDIASERVER_SYNC_INTERVAL,
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=5),
                kwargs={
                    'job_id': 'mediaserver_sync'
                }
            )

        # 新增订阅时搜索（5分钟检查一次）
        self._scheduler.add_job(
            self.start,
            "interval",
            minutes=5,
            kwargs={
                'job_id': 'subscribe_search',
                'state': 'N'
            }
        )

        # 检查更新订阅TMDB数据（每隔6小时）
        self._scheduler.add_job(
            self.start,
            "interval",
            id="subscribe_tmdb",
            name="订阅元数据更新",
            hours=6,
            kwargs={
                'job_id': 'subscribe_tmdb'
            }
        )

        # 订阅状态每隔24小时搜索一次
        if settings.SUBSCRIBE_SEARCH:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_search",
                name="订阅搜索",
                hours=24,
                kwargs={
                    'job_id': 'subscribe_search',
                    'state': 'R'
                }
            )

        if settings.SUBSCRIBE_MODE == "spider":
            # 站点首页种子定时刷新模式
            triggers = TimerUtils.random_scheduler(num_executions=30)
            for trigger in triggers:
                self._scheduler.add_job(
                    self.start,
                    "cron",
                    id=f"subscribe_refresh|{trigger.hour}:{trigger.minute}",
                    name="订阅刷新",
                    hour=trigger.hour,
                    minute=trigger.minute,
                    kwargs={
                        'job_id': 'subscribe_refresh'
                    })
        else:
            # RSS订阅模式
            if not settings.SUBSCRIBE_RSS_INTERVAL:
                settings.SUBSCRIBE_RSS_INTERVAL = 30
            elif settings.SUBSCRIBE_RSS_INTERVAL < 5:
                settings.SUBSCRIBE_RSS_INTERVAL = 5
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_refresh",
                name="RSS订阅刷新",
                minutes=settings.SUBSCRIBE_RSS_INTERVAL,
                kwargs={
                    'job_id': 'subscribe_refresh'
                }
            )

        # 下载器文件转移（每5分钟）
        if settings.DOWNLOADER_MONITOR:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="transfer",
                name="下载文件整理",
                minutes=5,
                kwargs={
                    'job_id': 'transfer'
                }
            )

        # 后台刷新TMDB壁纸
        self._scheduler.add_job(
            TmdbChain().get_random_wallpager,
            "interval",
            minutes=30,
            next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3)
        )

        # 公共定时服务
        self._scheduler.add_job(
            SchedulerChain().scheduler_job,
            "interval",
            minutes=10
        )

        # 缓存清理服务，每隔24小时
        self._scheduler.add_job(
            self.start,
            "interval",
            id="clear_cache",
            name="缓存清理",
            hours=settings.CACHE_CONF.get("meta") / 3600,
            kwargs={
                'job_id': 'clear_cache'
            }
        )

        # 打印服务
        logger.debug(self._scheduler.print_jobs())

        # 启动定时服务
        self._scheduler.start()

    def start(self, job_id: str, *args, **kwargs):
        """
        启动定时服务
        """
        # 处理job_id格式
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.get("running"):
            logger.warning(f"定时任务 {job_id} 正在运行 ...")
            return
        self._jobs[job_id]["running"] = True
        try:
            if not kwargs:
                kwargs = job.get("kwargs") or {}
            job["func"](*args, **kwargs)
        except Exception as e:
            logger.error(f"定时任务 {job_id} 执行失败：{str(e)}")
        self._jobs[job_id]["running"] = False

    def list(self) -> List[schemas.ScheduleInfo]:
        """
        当前所有任务
        """
        # 返回计时任务
        schedulers = []
        # 去重
        added = []
        jobs = self._scheduler.get_jobs()
        # 按照下次运行时间排序
        jobs.sort(key=lambda x: x.next_run_time)
        for job in jobs:
            if job.name not in added:
                added.append(job.name)
            else:
                continue
            job_id = job.id.split("|")[0]
            if not self._jobs.get(job_id):
                continue
            # 任务状态
            status = "正在运行" if self._jobs[job_id].get("running") else "等待"
            # 下次运行时间
            next_run = TimerUtils.time_difference(job.next_run_time)
            schedulers.append(schemas.ScheduleInfo(
                id=job_id,
                name=job.name,
                status=status,
                next_run=next_run
            ))
        return schedulers

    def stop(self):
        """
        关闭定时服务
        """
        self._event.set()
        if self._scheduler.running:
            self._scheduler.shutdown()
