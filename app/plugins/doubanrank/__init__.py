import datetime
import re
import xml.dom.minidom
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.douban import DoubanChain
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils


class DoubanRank(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣榜单订阅"
    # 插件描述
    plugin_desc = "监控豆瓣热门榜单，自动添加订阅。"
    # 插件图标
    plugin_icon = "movie.jpg"
    # 主题色
    plugin_color = "#01B3E3"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanrank_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()
    # 私有属性
    downloadchain: DownloadChain = None
    subscribechain: SubscribeChain = None
    doubanchain: DoubanChain = None
    _scheduler = None
    _douban_address = {
        'movie-ustop': 'https://rsshub.app/douban/movie/ustop',
        'movie-weekly': 'https://rsshub.app/douban/movie/weekly',
        'movie-real-time': 'https://rsshub.app/douban/movie/weekly/subject_real_time_hotest',
        'show-domestic': 'https://rsshub.app/douban/movie/weekly/show_domestic',
        'movie-hot-gaia': 'https://rsshub.app/douban/movie/weekly/movie_hot_gaia',
        'tv-hot': 'https://rsshub.app/douban/movie/weekly/tv_hot',
        'movie-top250': 'https://rsshub.app/douban/movie/weekly/movie_top250',
    }
    _enabled = False
    _cron = ""
    _onlyonce = False
    _rss_addrs = []
    _ranks = []
    _vote = 0
    _clear = False
    _clearflag = False

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.doubanchain = DoubanChain()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._vote = float(config.get("vote")) if config.get("vote") else 0
            rss_addrs = config.get("rss_addrs")
            if rss_addrs:
                if isinstance(rss_addrs, str):
                    self._rss_addrs = rss_addrs.split('\n')
                else:
                    self._rss_addrs = rss_addrs
            else:
                self._rss_addrs = []
            self._ranks = config.get("ranks") or []
            self._clear = config.get("clear")

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"豆瓣榜单订阅服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__refresh_rss,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="豆瓣榜单订阅")
                except Exception as e:
                    logger.error(f"豆瓣榜单订阅服务启动失败，错误信息：{str(e)}")
                    self.systemmessage.put(f"豆瓣榜单订阅服务启动失败，错误信息：{str(e)}")
            else:
                self._scheduler.add_job(func=self.__refresh_rss, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                logger.info("豆瓣榜单订阅服务启动，周期：每天 08:00")

            if self._onlyonce:
                logger.info("豆瓣榜单订阅服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__refresh_rss, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )

            if self._onlyonce or self._clear:
                # 关闭一次性开关
                self._onlyonce = False
                # 记录缓存清理标志
                self._clearflag = self._clear
                # 关闭清理缓存
                self._clear = False
                # 保存配置
                self.__update_config()

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'model': 'vote',
                                            'label': '评分',
                                            'placeholder': '评分大于等于该值才订阅'
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
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'ranks',
                                            'label': '热门榜单',
                                            'items': [
                                                {'title': '电影北美票房榜', 'value': 'movie-ustop'},
                                                {'title': '一周口碑电影榜', 'value': 'movie-weekly'},
                                                {'title': '实时热门电影', 'value': 'movie-real-time'},
                                                {'title': '热门综艺', 'value': 'show-domestic'},
                                                {'title': '热门电影', 'value': 'movie-hot-gaia'},
                                                {'title': '热门电视剧', 'value': 'tv-hot'},
                                                {'title': '电影TOP10', 'value': 'movie-top250'},
                                            ]
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
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rss_addrs',
                                            'label': '自定义榜单地址',
                                            'placeholder': '每行一个地址，如：https://rsshub.app/douban/movie/ustop'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理历史记录',
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
            "cron": "",
            "onlyonce": False,
            "vote": "",
            "ranks": [],
            "rss_addrs": "",
            "clear": False
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            doubanid = history.get("doubanid")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardSubtitle',
                                            'props': {
                                                'class': 'pa-2 font-bold break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'href': f"https://movie.douban.com/subject/{doubanid}",
                                                        'target': '_blank'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

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

    def __update_config(self):
        """
        列新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "vote": self._vote,
            "ranks": self._ranks,
            "rss_addrs": '\n'.join(map(str, self._rss_addrs)),
            "clear": self._clear
        })

    def __refresh_rss(self):
        """
        刷新RSS
        """
        logger.info(f"开始刷新豆瓣榜单 ...")
        addr_list = self._rss_addrs + [self._douban_address.get(rank) for rank in self._ranks]
        if not addr_list:
            logger.info(f"未设置榜单RSS地址")
            return
        else:
            logger.info(f"共 {len(addr_list)} 个榜单RSS地址需要刷新")

        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []

        for addr in addr_list:
            if not addr:
                continue
            try:
                logger.info(f"获取RSS：{addr} ...")
                rss_infos = self.__get_rss_info(addr)
                if not rss_infos:
                    logger.error(f"RSS地址：{addr} ，未查询到数据")
                    continue
                else:
                    logger.info(f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据")
                for rss_info in rss_infos:
                    if self._event.is_set():
                        logger.info(f"订阅服务停止")
                        return

                    title = rss_info.get('title')
                    douban_id = rss_info.get('doubanid')
                    unique_flag = f"doubanrank: {title} (DB:{douban_id})"
                    # 检查是否已处理过
                    if unique_flag in [h.get("unique") for h in history]:
                        continue
                    # 元数据
                    meta = MetaInfo(title)
                    # 识别媒体信息
                    if douban_id:
                        # 识别豆瓣信息
                        context = self.doubanchain.recognize_by_doubanid(douban_id)
                        mediainfo = context.media_info
                        if not mediainfo or not mediainfo.tmdb_id:
                            logger.warn(f'未识别到媒体信息，标题：{title}，豆瓣ID：{douban_id}')
                            continue

                    else:
                        # 匹配媒体信息
                        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                        if not mediainfo:
                            logger.warn(f'未识别到媒体信息，标题：{title}，豆瓣ID：{douban_id}')
                            continue
                    # 查询缺失的媒体信息
                    exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag:
                        logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                        continue
                    # 判断用户是否已经添加订阅
                    if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                        logger.info(f'{mediainfo.title_year} 订阅已存在')
                        continue
                    # 添加订阅
                    self.subscribechain.add(title=mediainfo.title,
                                            year=mediainfo.year,
                                            mtype=mediainfo.type,
                                            tmdbid=mediainfo.tmdb_id,
                                            season=meta.begin_season,
                                            exist_ok=True,
                                            username="豆瓣榜单")
                    # 存储历史记录
                    history.append({
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": mediainfo.tmdb_id,
                        "doubanid": douban_id,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "unique": unique_flag
                    })
            except Exception as e:
                logger.error(str(e))

        # 保存历史记录
        self.save_data('history', history)
        # 缓存只清理一次
        self._clearflag = False
        logger.info(f"所有榜单RSS刷新完成")

    @staticmethod
    def __get_rss_info(addr) -> List[dict]:
        """
        获取RSS
        """
        try:
            ret = RequestUtils().get_res(addr)
            if not ret:
                return []
            ret_xml = ret.text
            ret_array = []
            # 解析XML
            dom_tree = xml.dom.minidom.parseString(ret_xml)
            rootNode = dom_tree.documentElement
            items = rootNode.getElementsByTagName("item")
            for item in items:
                try:
                    # 标题
                    title = DomUtils.tag_value(item, "title", default="")
                    # 链接
                    link = DomUtils.tag_value(item, "link", default="")
                    if not title and not link:
                        logger.warn(f"条目标题和链接均为空，无法处理")
                        continue
                    doubanid = re.findall(r"/(\d+)/", link)
                    if doubanid:
                        doubanid = doubanid[0]
                    if doubanid and not str(doubanid).isdigit():
                        logger.warn(f"解析的豆瓣ID格式不正确：{doubanid}")
                        continue
                    # 返回对象
                    ret_array.append({
                        'title': title,
                        'link': link,
                        'doubanid': doubanid
                    })
                except Exception as e1:
                    logger.error("解析RSS条目失败：" + str(e1))
                    continue
            return ret_array
        except Exception as e:
            logger.error("获取RSS失败：" + str(e))
            return []
