import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from app import schemas
from app.chain import ChainBase
from app.chain.mediaserver import MediaServerChain
from app.chain.site import SiteChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import EventManager
from app.core.plugin import PluginManager
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.types import EventType
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
    _scheduler = None
    # 退出事件
    _event = threading.Event()
    # 锁
    _lock = threading.Lock()
    # 各服务的运行状态
    _jobs = {}
    # 用户认证失败次数
    _auth_count = 0

    def __init__(self):
        self.init()

    def init(self):
        """
        初始化定时服务
        """

        def clear_cache():
            """
            清理缓存
            """
            TorrentsChain().clear_cache()
            SchedulerChain().clear_cache()

        def user_auth():
            """
            用户认证检查
            """
            if SitesHelper().auth_level >= 2:
                return
            # 最大重试次数
            __max_try__ = 30
            if self._auth_count > __max_try__:
                SchedulerChain().messagehelper.put(title=f"用户认证失败",
                                                   message="用户认证失败次数过多，将不再尝试认证！",
                                                   role="system")
                return
            logger.info("用户未认证，正在尝试重新认证...")
            status, msg = SitesHelper().check_user()
            if status:
                self._auth_count = 0
                logger.info(f"{msg} 用户认证成功")
                SchedulerChain().post_message(
                    Notification(
                        mtype=NotificationType.Manual,
                        title="MoviePilot用户认证成功",
                        text=f"使用站点：{msg}",
                        link=settings.MP_DOMAIN('#/site')
                    )
                )
                PluginManager().init_config()
                for plugin_id in PluginManager().get_running_plugin_ids():
                    self.update_plugin_job(plugin_id)

            else:
                self._auth_count += 1
                logger.error(f"用户认证失败：{msg}，共失败 {self._auth_count} 次")
                if self._auth_count >= __max_try__:
                    logger.error("用户认证失败次数过多，将不再尝试认证！")

        # 各服务的运行状态
        self._jobs = {
            "cookiecloud": {
                "name": "同步CookieCloud站点",
                "func": SiteChain().sync_cookies,
                "running": False,
            },
            "mediaserver_sync": {
                "name": "同步媒体服务器",
                "func": MediaServerChain().sync,
                "running": False,
            },
            "subscribe_tmdb": {
                "name": "订阅元数据更新",
                "func": SubscribeChain().check,
                "running": False,
            },
            "subscribe_search": {
                "name": "订阅搜索补全",
                "func": SubscribeChain().search,
                "running": False,
                "kwargs": {
                    "state": "R"
                }
            },
            "new_subscribe_search": {
                "name": "新增订阅搜索",
                "func": SubscribeChain().search,
                "running": False,
                "kwargs": {
                    "state": "N"
                }
            },
            "subscribe_refresh": {
                "name": "订阅刷新",
                "func": SubscribeChain().refresh,
                "running": False,
            },
            "transfer": {
                "name": "下载文件整理",
                "func": TransferChain().process,
                "running": False,
            },
            "clear_cache": {
                "name": "缓存清理",
                "func": clear_cache,
                "running": False,
            },
            "user_auth": {
                "name": "用户认证检查",
                "func": user_auth,
                "running": False,
            },
            "scheduler_job": {
                "name": "公共定时服务",
                "func": SchedulerChain().scheduler_job,
                "running": False,
            },
            "random_wallpager": {
                "name": "壁纸缓存",
                "func": TmdbChain().get_trending_wallpapers,
                "running": False,
            }
        }

        # 停止定时服务
        self.stop()

        # 调试模式不启动定时服务
        if settings.DEV:
            return

        # 创建定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ,
                                              executors={
                                                  'default': ThreadPoolExecutor(100)
                                              })

        # CookieCloud定时同步
        if settings.COOKIECLOUD_INTERVAL \
                and str(settings.COOKIECLOUD_INTERVAL).isdigit():
            self._scheduler.add_job(
                self.start,
                "interval",
                id="cookiecloud",
                name="同步CookieCloud站点",
                minutes=int(settings.COOKIECLOUD_INTERVAL),
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=1),
                kwargs={
                    'job_id': 'cookiecloud'
                }
            )

        # 媒体服务器同步
        if settings.MEDIASERVER_SYNC_INTERVAL \
                and str(settings.MEDIASERVER_SYNC_INTERVAL).isdigit():
            self._scheduler.add_job(
                self.start,
                "interval",
                id="mediaserver_sync",
                name="同步媒体服务器",
                hours=int(settings.MEDIASERVER_SYNC_INTERVAL),
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=5),
                kwargs={
                    'job_id': 'mediaserver_sync'
                }
            )

        # 新增订阅时搜索（5分钟检查一次）
        self._scheduler.add_job(
            self.start,
            "interval",
            id="new_subscribe_search",
            name="新增订阅搜索",
            minutes=5,
            kwargs={
                'job_id': 'new_subscribe_search'
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
                name="订阅搜索补全",
                hours=24,
                kwargs={
                    'job_id': 'subscribe_search'
                }
            )

        if settings.SUBSCRIBE_MODE == "spider":
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
                    kwargs={
                        'job_id': 'subscribe_refresh'
                    })
        else:
            # RSS订阅模式
            if not settings.SUBSCRIBE_RSS_INTERVAL \
                    or not str(settings.SUBSCRIBE_RSS_INTERVAL).isdigit():
                settings.SUBSCRIBE_RSS_INTERVAL = 30
            elif int(settings.SUBSCRIBE_RSS_INTERVAL) < 5:
                settings.SUBSCRIBE_RSS_INTERVAL = 5
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_refresh",
                name="RSS订阅刷新",
                minutes=int(settings.SUBSCRIBE_RSS_INTERVAL),
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
            self.start,
            "interval",
            id="random_wallpager",
            name="壁纸缓存",
            minutes=30,
            next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            kwargs={
                'job_id': 'random_wallpager'
            }
        )

        # 公共定时服务
        self._scheduler.add_job(
            self.start,
            "interval",
            id="scheduler_job",
            name="公共定时服务",
            minutes=10,
            kwargs={
                'job_id': 'scheduler_job'
            }
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

        # 定时检查用户认证，每隔10分钟
        self._scheduler.add_job(
            self.start,
            "interval",
            id="user_auth",
            name="用户认证检查",
            minutes=10,
            kwargs={
                'job_id': 'user_auth'
            }
        )

        # 注册插件公共服务
        for pid in PluginManager().get_running_plugin_ids():
            self.update_plugin_job(pid)

        # 打印服务
        logger.debug(self._scheduler.print_jobs())

        # 启动定时服务
        self._scheduler.start()

    def start(self, job_id: str, *args, **kwargs):
        """
        启动定时服务
        """
        # 处理job_id格式
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job_name = job.get("name")
            if job.get("running"):
                logger.warning(f"定时任务 {job_id} - {job_name} 正在运行 ...")
                return
            self._jobs[job_id]["running"] = True
        # 开始运行
        try:
            if not kwargs:
                kwargs = job.get("kwargs") or {}
            job["func"](*args, **kwargs)
        except Exception as e:
            logger.error(f"定时任务 {job_name} 执行失败：{str(e)} - {traceback.format_exc()}")
            SchedulerChain().messagehelper.put(title=f"{job_name} 执行失败",
                                               message=str(e),
                                               role="system")
            EventManager().send_event(
                EventType.SystemError,
                {
                    "type": "scheduler",
                    "scheduler_id": job_id,
                    "scheduler_name": job_name,
                    "error": str(e),
                    "traceback": traceback.format_exc()
                }
            )
        # 运行结束
        with self._lock:
            try:
                self._jobs[job_id]["running"] = False
            except KeyError:
                pass

    def update_plugin_job(self, pid: str):
        """
        更新插件定时服务
        """
        if not self._scheduler:
            return
        # 移除该插件的全部服务
        self.remove_plugin_job(pid)
        # 获取插件服务列表
        with self._lock:
            try:
                plugin_services = PluginManager().run_plugin_method(pid, "get_service") or []
            except Exception as e:
                logger.error(f"运行插件 {pid} 服务失败：{str(e)} - {traceback.format_exc()}")
                return
            # 获取插件名称
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
            # 开始注册插件服务
            for service in plugin_services:
                try:
                    sid = f"{service['id']}"
                    job_id = sid.split("|")[0]
                    if job_id not in self._jobs:
                        self._jobs[job_id] = {
                            "func": service["func"],
                            "name": service["name"],
                            "pid": pid,
                            "plugin_name": plugin_name,
                            "running": False,
                        }
                        self._scheduler.add_job(
                            self.start,
                            service["trigger"],
                            id=sid,
                            name=service["name"],
                            **service["kwargs"],
                            kwargs={
                                'job_id': job_id
                            }
                        )
                        logger.info(f"注册插件{plugin_name}服务：{service['name']} - {service['trigger']}")
                except Exception as e:
                    logger.error(f"注册插件{plugin_name}服务失败：{str(e)} - {service}")
                    SchedulerChain().messagehelper.put(title=f"插件 {plugin_name} 服务注册失败",
                                                       message=str(e),
                                                       role="system")

    def remove_plugin_job(self, pid: str):
        """
        移除插件定时服务
        """
        if not self._scheduler:
            return
        with self._lock:
            # 获取插件名称
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
            for job_id, service in self._jobs.copy().items():
                try:
                    if service.get("pid") == pid:
                        self._jobs.pop(job_id, None)
                        try:
                            self._scheduler.remove_job(job_id)
                        except JobLookupError:
                            pass
                        logger.info(f"移除插件服务({plugin_name})：{service.get('name')}")
                except Exception as e:
                    logger.error(f"移除插件服务失败：{str(e)} - {job_id}: {service}")
                    SchedulerChain().messagehelper.put(title=f"插件 {plugin_name} 服务移除失败",
                                                       message=str(e),
                                                       role="system")

    def list(self) -> List[schemas.ScheduleInfo]:
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
            jobs = self._scheduler.get_jobs()
            # 按照下次运行时间排序
            jobs.sort(key=lambda x: x.next_run_time)
            # 将正在运行的任务提取出来 (保障一次性任务正常显示)
            for job_id, service in self._jobs.items():
                name = service.get("name")
                plugin_name = service.get("plugin_name")
                if service.get("running") and name and plugin_name:
                    if name not in added:
                        added.append(name)
                    schedulers.append(schemas.ScheduleInfo(
                        id=job_id,
                        name=name,
                        provider=plugin_name,
                        status="正在运行",
                    ))
            # 获取其他待执行任务
            for job in jobs:
                if job.name not in added:
                    added.append(job.name)
                else:
                    continue
                job_id = job.id.split("|")[0]
                service = self._jobs.get(job_id)
                if not service:
                    continue
                # 任务状态
                status = "正在运行" if service.get("running") else "等待"
                # 下次运行时间
                next_run = TimerUtils.time_difference(job.next_run_time)
                schedulers.append(schemas.ScheduleInfo(
                    id=job_id,
                    name=job.name,
                    provider=service.get("plugin_name", "[系统]"),
                    status=status,
                    next_run=next_run
                ))
            return schedulers

    def stop(self):
        """
        关闭定时服务
        """
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
