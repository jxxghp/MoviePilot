import threading
import time
from typing import Any, List, Dict, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo, MediaServerItem
from app.schemas.types import EventType, MediaType
from app.utils.string import StringUtils


class PersonMeta(_PluginBase):
    # 插件名称
    plugin_name = "演职人员刮削"
    # 插件描述
    plugin_desc = "刮削演职人员图片以及中文名称。"
    # 插件图标
    plugin_icon = "actor.png"
    # 主题色
    plugin_color = "#E66E72"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "personmeta_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = threading.Event()

    # 私有属性
    _scheduler = None
    tmdbchain = None
    mschain = None
    _enabled = False
    _onlyonce = False
    _cron = None
    _delay = 0

    def init_plugin(self, config: dict = None):
        self.tmdbchain = TmdbChain(self.db)
        self.mschain = MediaServerChain(self.db)
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._delay = config.get("delay") or 0

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"演职人员刮削服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.scrap_library,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="演职人员刮削")
                except Exception as e:
                    logger.error(f"演职人员刮削服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"演职人员刮削服务启动失败，错误信息：{str(e)}")

            if self._onlyonce:
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "delay": self._delay
        })

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
                                    'md': 6
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '媒体库扫描周期',
                                            'placeholder': '5位cron表达式'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay',
                                            'label': '入库延迟时间（秒）',
                                            'placeholder': '30'
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
            "onlyonce": False,
            "cron": "",
            "delay": 30
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def scrap_rt(self, event: Event):
        """
        根据事件实时刮削演员信息
        """
        if not self._enabled:
            return
        # 事件数据
        mediainfo: MediaInfo = event.event_data.get("mediainfo")
        if not mediainfo:
            return
        # 延迟
        if self._delay:
            time.sleep(int(self._delay))
        # 查询媒体服务器中的条目
        existsinfo = self.chain.media_exists(mediainfo=mediainfo)
        if not existsinfo or not existsinfo.itemid:
            logger.warn(f"演职人员刮削 {mediainfo.title_year} 在媒体库中不存在")
            return
        # 初始化媒体服务器
        if existsinfo.server == "plex":
            logger.warn(f"演职人员刮削 不支持{existsinfo.server}媒体服务器")
            return
        # 查询条目详情
        iteminfo = self.mschain.iteminfo(server=existsinfo.server, item_id=existsinfo.itemid)
        if not iteminfo:
            logger.warn(f"演职人员刮削 {mediainfo.title_year} 条目详情获取失败")
            return
        # 刮削演职人员信息
        self.__update_item(server=existsinfo.server, item=iteminfo, mediainfo=mediainfo)

    def scrap_library(self):
        """
        扫描整个媒体库，刮削演员信息
        """
        # 所有媒体服务器
        if not settings.MEDIASERVER:
            return
        for server in settings.MEDIASERVER.split(","):
            if server == "plex":
                logger.warn(f"演职人员刮削 不支持{server}媒体服务器")
                continue
            # 扫描所有媒体库
            logger.info(f"开始刮削服务器 {server} 的演员信息 ...")
            for library in self.mschain.librarys(server):
                logger.info(f"开始刮削媒体库 {library.name} 的演员信息 ...")
                for item in self.mschain.items(server, library.id):
                    if not item:
                        continue
                    if not item.item_id:
                        continue
                    if self._event.is_set():
                        logger.info(f"演职人员刮削服务停止")
                        return
                    # 处理条目
                    logger.info(f"开始刮削 {item.title} 的演员信息 ...")
                    self.__update_item(server=server, item=item)
                    logger.info(f"{item.title} 的演员信息刮削完成")
                logger.info(f"媒体库 {library.name} 的演员信息刮削完成")
            logger.info(f"服务器 {server} 的演员信息刮削完成")

    def __update_item(self, server: str, item: MediaServerItem, mediainfo: MediaInfo = None):
        """
        更新媒体服务器中的条目
        """
        # 识别媒体信息
        if not mediainfo:
            if not item.tmdbid:
                logger.warn(f"{item.title} 未找到tmdbid，无法识别媒体信息")
                return
            mtype = MediaType.TV if item.item_type in ['Series', 'show'] else MediaType.MOVIE
            mediainfo = self.chain.recognize_media(mtype=mtype, tmdbid=item.tmdbid)
            if not mediainfo:
                logger.warn(f"{item.title} 未识别到媒体信息")
                return
        # 搜索豆瓣词条

        # 搜索豆瓣人物信息

        # 匹配非中文人名

        # 更新中文人名

        # 下载图片

        # 更新演员图片
        pass

    def __get_chinese_name(self, person: dict):
        """
        获取TMDB别名中的中文名
        """
        if not person.get("id"):
            return ""
        try:
            personinfo = self.tmdbchain.person_detail(person.get("id"))
            if personinfo:
                also_known_as = personinfo.get("also_known_as") or []
                if also_known_as:
                    for name in also_known_as:
                        if name and StringUtils.is_chinese(name):
                            return name
        except Exception as err:
            logger.error(f"获取人物中文名失败：{err}")
        return person.get("name") or ""

    def stop_service(self):
        """
        停止服务
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
