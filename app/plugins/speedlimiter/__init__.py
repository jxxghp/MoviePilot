from typing import List, Tuple, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class SpeedLimiter(_PluginBase):
    # 插件名称
    plugin_name = "播放限速"
    # 插件描述
    plugin_desc = "媒体服务器播通过外网播放时，自动对下载器进行限速。"
    # 插件图标
    plugin_icon = "SpeedLimiter.jpg"
    # 主题色
    plugin_color = "#183883"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "Shurelol"
    # 作者主页
    author_url = "https://github.com/Shurelol"
    # 插件配置项ID前缀
    plugin_config_prefix = "speedlimit_"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    _enable: bool = False
    _notify: bool = False
    _bandwidth: int = 0
    _interval: int = 60

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enable = config.get("enable")
            self._notify = config.get("notify")
            try:
                # 总带宽
                self._bandwidth = int(float(config.get("bandwidth") or 0)) * 1000000
            except Exception as e:
                logger.error(f"总带宽配置错误：{e}")
                self._bandwidth = 0

        # 移出现有任务
        self.stop_service()

        # 启动限速任务
        if self._enable:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(func=self.__check_playing_sessions,
                                    trigger='interval',
                                    seconds=self._interval)
            self._scheduler.print_jobs()
            self._scheduler.start()
            logger.info("播放限速服务启动")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    def __check_playing_sessions(self):
        """
        检查播放会话
        """
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            print(str(e))
