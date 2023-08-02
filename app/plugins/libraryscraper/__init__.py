from threading import Event
from typing import List, Tuple, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class LibraryScraper(_PluginBase):

    # 插件名称
    plugin_name = "媒体库刮削"
    # 插件描述
    plugin_desc = "定时对媒体库进行刮削，补齐缺失元数据和图片。"
    # 插件图标
    plugin_icon = "scraper.png"
    # 主题色
    plugin_color = "#FF7D00"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "libraryscraper_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    user_level = 1

    # 私有属性
    _scheduler = None
    _scraper = None
    # 限速开关
    _enable = False
    _cron = None
    _mode = None
    _scraper_path = None
    _exclude_path = None
    # 退出事件
    _event = Event()
    
    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enable = config.get("enable")
            self._cron = config.get("cron")
            self._mode = config.get("mode")
            self._scraper_path = config.get("scraper_path")
            self._exclude_path = config.get("exclude_path")

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self._enable:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"媒体库刮削服务启动，周期：{self._cron}")
                self._scheduler.add_job(self.__libraryscraper,
                                        CronTrigger.from_crontab(self._cron))
            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    def __libraryscraper(self):
        """
        开始刮削媒体库
        """
        # 已选择的目录
        logger.info(f"开始刮削媒体库：{self._scraper_path} ...")
        for path in self._scraper_path:
            if not path:
                continue
            if self._event.is_set():
                logger.info(f"媒体库刮削服务停止")
                return
            # TODO 刮削目录
        logger.info(f"媒体库刮削完成")

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
