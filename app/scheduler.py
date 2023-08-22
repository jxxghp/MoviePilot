import logging
from datetime import datetime, timedelta

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.chain import ChainBase
from app.chain.cookiecloud import CookieCloudChain
from app.chain.mediaserver import MediaServerChain
from app.chain.rss import RssChain
from app.chain.subscribe import SubscribeChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.db import SessionLocal
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

    def __init__(self):
        # 数据库连接
        self._db = SessionLocal()
        # 调试模式不启动定时服务
        if settings.DEV:
            return
        # CookieCloud定时同步
        if settings.COOKIECLOUD_INTERVAL:
            self._scheduler.add_job(CookieCloudChain(self._db).process,
                                    "interval",
                                    minutes=settings.COOKIECLOUD_INTERVAL,
                                    next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=1),
                                    name="同步CookieCloud站点")

        # 媒体服务器同步
        if settings.MEDIASERVER_SYNC_INTERVAL:
            self._scheduler.add_job(MediaServerChain(self._db).sync, "interval",
                                    hours=settings.MEDIASERVER_SYNC_INTERVAL,
                                    next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=5),
                                    name="同步媒体服务器")

        # 新增订阅时搜索（5分钟检查一次）
        self._scheduler.add_job(SubscribeChain(self._db).search, "interval",
                                minutes=5, kwargs={'state': 'N'})

        # 订阅状态每隔12小时搜索一次
        self._scheduler.add_job(SubscribeChain(self._db).search, "interval",
                                hours=12, kwargs={'state': 'R'}, name="订阅搜索")

        # 站点首页种子定时刷新缓存并匹配订阅
        triggers = TimerUtils.random_scheduler(num_executions=30)
        for trigger in triggers:
            self._scheduler.add_job(SubscribeChain(self._db).refresh, "cron",
                                    hour=trigger.hour, minute=trigger.minute, name="订阅刷新")

        # 自定义订阅
        self._scheduler.add_job(RssChain(self._db).refresh, "interval",
                                minutes=30, name="自定义订阅刷新")

        # 下载器文件转移（每5分钟）
        if settings.DOWNLOADER_MONITOR:
            self._scheduler.add_job(TransferChain(self._db).process, "interval", minutes=5, name="下载文件整理")

        # 公共定时服务
        self._scheduler.add_job(SchedulerChain(self._db).scheduler_job, "interval", minutes=10)

        # 打印服务
        logger.debug(self._scheduler.print_jobs())

        # 启动定时服务
        self._scheduler.start()

    def list(self):
        """
        当前所有任务
        """
        return self._scheduler.get_jobs()

    def stop(self):
        """
        关闭定时服务
        """
        if self._scheduler.running:
            self._scheduler.shutdown()

    def __del__(self):
        if self._db:
            self._db.close()
