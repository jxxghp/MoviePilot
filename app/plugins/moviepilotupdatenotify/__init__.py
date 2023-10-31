import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.system import SystemChain
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils


class MoviePilotUpdateNotify(_PluginBase):
    # 插件名称
    plugin_name = "MoviePilot更新推送"
    # 插件描述
    plugin_desc = "MoviePilot推送release更新通知、自动重启。"
    # 插件图标
    plugin_icon = "update.png"
    # 主题色
    plugin_color = "#4179F4"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "moviepilotupdatenotify_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _restart = False
    _notify = False

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._restart = config.get("restart")
            self._notify = config.get("notify")

            # 加载模块
        if self._enabled:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    self._scheduler.add_job(func=self.__check_update,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="检查MoviePilot更新")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __check_update(self):
        """
        检查MoviePilot更新
        """
        release_version, description, update_time = self.__get_release_version()
        if not release_version:
            logger.error("最新版本获取失败，停止运行")
            return

        # 本地版本
        local_version = SystemChain().get_local_version()
        if local_version and release_version <= local_version:
            logger.info(f"当前版本：{local_version} 远程版本：{release_version} 停止运行")
            return

        # 推送更新消息
        if self._notify:
            # 将时间字符串转为datetime对象
            dt = datetime.datetime.strptime(update_time, "%Y-%m-%dT%H:%M:%SZ")
            # 设置时区
            timezone = pytz.timezone(settings.TZ)
            dt = dt.replace(tzinfo=timezone)
            # 将datetime对象转换为带时区的字符串
            update_time = dt.strftime("%Y-%m-%d %H:%M:%S")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【MoviePilot更新通知】",
                text=f"{release_version} \n"
                     f"\n"
                     f"{description} \n"
                     f"\n"
                     f"{update_time}")

        # 自动重启
        if self._restart:
            logger.info("开始执行自动重启…")
            SystemUtils.restart()

    @staticmethod
    def __get_release_version():
        """
        获取最新版本
        """
        version_res = RequestUtils(proxies=settings.PROXY).get_res(
            "https://api.github.com/repos/jxxghp/MoviePilot/releases/latest")
        if version_res:
            ver_json = version_res.json()
            version = f"{ver_json['tag_name']}"
            description = f"{ver_json['body']}"
            update_time = f"{ver_json['published_at']}"
            return version, description, update_time
        else:
            return None, None, None

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
                   {
                       'component': 'VForm',
                       'content': [
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'enabled',
                                                   'label': '启用插件',
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'restart',
                                                   'label': '自动重启',
                                               }
                                           }
                                       ]
                                   },
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'notify',
                                                   'label': '发送通知',
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'cron',
                                                   'label': '检查周期',
                                                   'placeholder': '5位cron表达式'
                                               }
                                           }
                                       ]
                                   },
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VAlert',
                                               'props': {
                                                   'type': 'info',
                                                   'variant': 'tonal',
                                                   'text': '如要开启自动重启，请确认MOVIEPILOT_AUTO_UPDATE设置为true，重启即更新。'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           }
                       ]
                   }
               ], {
                   "enabled": False,
                   "restart": False,
                   "notify": False,
                   "cron": "0 9 * * *"
               }

    def get_page(self) -> List[dict]:
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
            logger.error("退出插件失败：%s" % str(e))
