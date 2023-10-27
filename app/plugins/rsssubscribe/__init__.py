import datetime
import re
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import SystemConfigKey, MediaType

lock = Lock()


class RssSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "自定义订阅"
    # 插件描述
    plugin_desc = "定时刷新RSS报文，识别内容后添加订阅或直接下载。"
    # 插件图标
    plugin_icon = "rss.png"
    # 主题色
    plugin_color = "#F78421"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "rsssubscribe_"
    # 加载顺序
    plugin_order = 19
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    rsshelper = None
    downloadchain = None
    searchchain = None
    subscribechain = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _notify: bool = False
    _onlyonce: bool = False
    _address: str = ""
    _include: str = ""
    _exclude: str = ""
    _proxy: bool = False
    _filter: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _action: str = "subscribe"
    _save_path: str = ""

    def init_plugin(self, config: dict = None):
        self.rsshelper = RssHelper()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._address = config.get("address")
            self._include = config.get("include")
            self._exclude = config.get("exclude")
            self._proxy = config.get("proxy")
            self._filter = config.get("filter")
            self._clear = config.get("clear")
            self._action = config.get("action")
            self._save_path = config.get("save_path")

        if self._enabled or self._onlyonce:

            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.check,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="RSS订阅")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{str(err)}")
            else:
                self._scheduler.add_job(self.check, "interval", minutes=30, name="RSS订阅")

            if self._onlyonce:
                logger.info(f"RSS订阅服务启动，立即运行一次")
                self._scheduler.add_job(func=self.check, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )

            if self._onlyonce or self._clear:
                # 关闭一次性开关
                self._onlyonce = False
                # 记录清理缓存设置
                self._clearflag = self._clear
                # 关闭清理缓存开关
                self._clear = False
                # 保存设置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'action',
                                            'label': '动作',
                                            'items': [
                                                {'title': '订阅', 'value': 'subscribe'},
                                                {'title': '下载', 'value': 'download'}
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'address',
                                            'label': 'RSS地址',
                                            'rows': 3,
                                            'placeholder': '每行一个RSS地址'
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
                                            'model': 'include',
                                            'label': '包含',
                                            'placeholder': '支持正则表达式'
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
                                            'model': 'exclude',
                                            'label': '排除',
                                            'placeholder': '支持正则表达式'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_path',
                                            'label': '保存目录',
                                            'placeholder': '下载时有效，留空自动'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '使用代理服务器',
                                        }
                                    }
                                ]
                            }, {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'filter',
                                            'label': '使用过滤规则',
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
            "notify": True,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "address": "",
            "include": "",
            "exclude": "",
            "proxy": False,
            "clear": False,
            "filter": False,
            "action": "subscribe",
            "save_path": ""
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
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
                                            'text': title
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

    def __update_config(self):
        """
        更新设置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "address": self._address,
            "include": self._include,
            "exclude": self._exclude,
            "proxy": self._proxy,
            "clear": self._clear,
            "filter": self._filter,
            "action": self._action,
            "save_path": self._save_path
        })

    def check(self):
        """
        通过用户RSS同步豆瓣想看数据
        """
        if not self._address:
            return
        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []
        for url in self._address.split("\n"):
            # 处理每一个RSS链接
            if not url:
                continue
            logger.info(f"开始刷新RSS：{url} ...")
            results = self.rsshelper.parse(url, proxy=self._proxy)
            if not results:
                logger.error(f"未获取到RSS数据：{url}")
                return
            # 过滤规则
            filter_rule = self.systemconfig.get(SystemConfigKey.SubscribeFilterRules)
            # 解析数据
            for result in results:
                try:
                    title = result.get("title")
                    description = result.get("description")
                    enclosure = result.get("enclosure")
                    link = result.get("link")
                    sise = result.get("sise")
                    pubdate: datetime.datetime = result.get("pubdate")
                    # 检查是否处理过
                    if not title or title in [h.get("key") for h in history]:
                        continue
                    # 检查规则
                    if self._include and not re.search(r"%s" % self._include,
                                                       f"{title} {description}", re.IGNORECASE):
                        logger.info(f"{title} - {description} 不符合包含规则")
                        continue
                    if self._exclude and re.search(r"%s" % self._exclude,
                                                   f"{title} {description}", re.IGNORECASE):
                        logger.info(f"{title} - {description} 不符合排除规则")
                        continue
                    # 识别媒体信息
                    meta = MetaInfo(title=title, subtitle=description)
                    if not meta.name:
                        logger.warn(f"{title} 未识别到有效数据")
                        continue
                    mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{title}')
                        continue
                    # 种子
                    torrentinfo = TorrentInfo(
                        title=title,
                        description=description,
                        enclosure=enclosure,
                        page_url=link,
                        size=sise,
                        pubdate=pubdate.strftime("%Y-%m-%d %H:%M:%S") if pubdate else None,
                        site_proxy=self._proxy,
                    )
                    # 过滤种子
                    if self._filter:
                        result = self.chain.filter_torrents(
                            rule_string=filter_rule,
                            torrent_list=[torrentinfo],
                            mediainfo=mediainfo
                        )
                        if not result:
                            logger.info(f"{title} {description} 不匹配过滤规则")
                            continue
                    # 查询缺失的媒体信息
                    exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag:
                        logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                        continue
                    else:
                        if self._action == "download":
                            if mediainfo.type == MediaType.TV:
                                if no_exists:
                                    exist_info = no_exists.get(mediainfo.tmdb_id)
                                    season_info = exist_info.get(meta.begin_season or 1)
                                    if not season_info:
                                        logger.info(f'{mediainfo.title_year} {meta.season} 己存在')
                                        continue
                                    if (season_info.episodes
                                            and not set(meta.episode_list).issubset(set(season_info.episodes))):
                                        logger.info(f'{mediainfo.title_year} {meta.season_episode} 己存在')
                                        continue
                            # 添加下载
                            result = self.downloadchain.download_single(
                                context=Context(
                                    meta_info=meta,
                                    media_info=mediainfo,
                                    torrent_info=torrentinfo,
                                ),
                                save_path=self._save_path,
                                username="RSS订阅"
                            )
                            if not result:
                                logger.error(f'{title} 下载失败')
                                continue
                        else:
                            # 检查是否在订阅中
                            subflag = self.subscribechain.exists(mediainfo=mediainfo, meta=meta)
                            if subflag:
                                logger.info(f'{mediainfo.title_year} {meta.season} 正在订阅中')
                                continue
                            # 添加订阅
                            self.subscribechain.add(title=mediainfo.title,
                                                    year=mediainfo.year,
                                                    mtype=mediainfo.type,
                                                    tmdbid=mediainfo.tmdb_id,
                                                    season=meta.begin_season,
                                                    exist_ok=True,
                                                    username="RSS订阅")
                    # 存储历史记录
                    history.append({
                        "title": f"{mediainfo.title} {meta.season}",
                        "key": f"{title}",
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": mediainfo.tmdb_id,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                except Exception as err:
                    logger.error(f'刷新RSS数据出错：{str(err)}')
            logger.info(f"RSS {url} 刷新完成")
        # 保存历史记录
        self.save_data('history', history)
        # 缓存只清理一次
        self._clearflag = False
