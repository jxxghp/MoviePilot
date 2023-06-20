import logging
from datetime import datetime, timedelta

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.chain import ChainBase
from app.chain.cookiecloud import CookieCloudChain
from app.chain.douban import DoubanChain
from app.chain.subscribe import SubscribeChain
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

    def __init__(self):
        # CookieCloud定时同步
        if settings.COOKIECLOUD_INTERVAL:
            self._scheduler.add_job(CookieCloudChain().process,
                                    "interval",
                                    minutes=settings.COOKIECLOUD_INTERVAL,
                                    next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=1))

        # 新增订阅时搜索（5分钟检查一次）
        self._scheduler.add_job(SubscribeChain().search, "interval", minutes=5, kwargs={'state': 'N'})

        # 订阅状态每隔12小时刷新一次
        self._scheduler.add_job(SubscribeChain().search, "interval", hours=12, kwargs={'state': 'R'})

        # 站点首页种子定时刷新缓存并匹配订阅
        triggers = TimerUtils.random_scheduler(num_executions=20)
        for trigger in triggers:
            self._scheduler.add_job(SubscribeChain().refresh, "cron", hour=trigger.hour, minute=trigger.minute)

        # 豆瓣同步（每30分钟）
        self._scheduler.add_job(DoubanChain().sync, "interval", minutes=30)

        # 下载器文件转移（每5分钟）
        self._scheduler.add_job(TransferChain().process, "interval", minutes=5)

        # 公共定时服务
        self._scheduler.add_job(SchedulerChain().scheduler_job, "interval", minutes=10)

        # 打印服务
        logger.debug(self._scheduler.print_jobs())

        # 启动定时服务
        self._scheduler.start()

    def stop(self):
        """
        关闭定时服务
        """
        if self._scheduler.running:
            self._scheduler.shutdown()
