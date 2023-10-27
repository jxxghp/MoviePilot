import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.douban import DoubanChain
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

lock = Lock()


class DoubanSync(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣想看"
    # 插件描述
    plugin_desc = "同步豆瓣想看数据，自动添加订阅。"
    # 插件图标
    plugin_icon = "douban.png"
    # 主题色
    plugin_color = "#05B711"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubansync_"
    # 加载顺序
    plugin_order = 3
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _interests_url: str = "https://www.douban.com/feed/people/%s/interests"
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    rsshelper = None
    downloadchain = None
    searchchain = None
    subscribechain = None
    doubanchain = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _days: int = 7
    _users: str = ""
    _clear: bool = False
    _clearflag: bool = False

    def init_plugin(self, config: dict = None):
        self.rsshelper = RssHelper()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()
        self.doubanchain = DoubanChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._days = config.get("days")
            self._users = config.get("users")
            self._onlyonce = config.get("onlyonce")
            self._clear = config.get("clear")

        if self._enabled or self._onlyonce:

            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.sync,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="豆瓣想看")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{str(err)}")
            else:
                self._scheduler.add_job(self.sync, "interval", minutes=30, name="豆瓣想看")

            if self._onlyonce:
                logger.info(f"豆瓣想看服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync, trigger='date',
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
        return [{
            "cmd": "/douban_sync",
            "event": EventType.DoubanSync,
            "desc": "同步豆瓣想看",
            "category": "订阅",
            "data": {}
        }]

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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'days',
                                            'label': '同步天数'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'users',
                                            'label': '用户列表',
                                            'placeholder': '豆瓣用户ID，多个用英文逗号分隔'
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
            "notify": True,
            "onlyonce": False,
            "cron": "*/30 * * * *",
            "days": 7,
            "users": "",
            "clear": False
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

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "users": self._users,
            "clear": self._clear
        })

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

    def sync(self):
        """
        通过用户RSS同步豆瓣想看数据
        """
        if not self._users:
            return
        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []
        for user_id in self._users.split(","):
            # 同步每个用户的豆瓣数据
            if not user_id:
                continue
            logger.info(f"开始同步用户 {user_id} 的豆瓣想看数据 ...")
            url = self._interests_url % user_id
            results = self.rsshelper.parse(url)
            if not results:
                logger.warn(f"未获取到用户 {user_id} 豆瓣RSS数据：{url}")
                continue
            else:
                logger.info(f"获取到用户 {user_id} 豆瓣RSS数据：{len(results)}")
            # 解析数据
            for result in results:
                try:
                    dtype = result.get("title", "")[:2]
                    title = result.get("title", "")[2:]
                    if dtype not in ["想看", "在看"]:
                        logger.info(f'标题：{title}，非想看/在看数据，跳过')
                        continue
                    if not result.get("link"):
                        logger.warn(f'标题：{title}，未获取到链接，跳过')
                        continue
                    # 判断是否在天数范围
                    pubdate: Optional[datetime.datetime] = result.get("pubdate")
                    if pubdate:
                        if (datetime.datetime.now(datetime.timezone.utc) - pubdate).days > float(self._days):
                            logger.info(f'已超过同步天数，标题：{title}，发布时间：{pubdate}')
                            continue
                    douban_id = result.get("link", "").split("/")[-2]
                    # 检查是否处理过
                    if not douban_id or douban_id in [h.get("doubanid") for h in history]:
                        logger.info(f'标题：{title}，豆瓣ID：{douban_id} 已处理过')
                        continue
                    # 识别媒体信息
                    meta = MetaInfo(title=title)
                    context = self.doubanchain.recognize_by_doubanid(douban_id)
                    mediainfo = context.media_info
                    if not mediainfo or not mediainfo.tmdb_id:
                        logger.warn(f'未识别到媒体信息，标题：{title}，豆瓣ID：{douban_id}')
                        continue
                    # 查询缺失的媒体信息
                    exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag:
                        logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                        action = "exist"
                    else:
                        logger.info(f'{mediainfo.title_year} 媒体库中不存在，开始搜索 ...')
                        # 搜索
                        contexts = self.searchchain.process(mediainfo=mediainfo,
                                                            no_exists=no_exists)
                        if not contexts:
                            logger.warn(f'{mediainfo.title_year} 未搜索到资源')
                            # 添加订阅
                            self.subscribechain.add(title=mediainfo.title,
                                                    year=mediainfo.year,
                                                    mtype=mediainfo.type,
                                                    tmdbid=mediainfo.tmdb_id,
                                                    season=meta.begin_season,
                                                    exist_ok=True,
                                                    username="豆瓣想看")
                            action = "subscribe"
                        else:
                            # 自动下载
                            downloads, lefts = self.downloadchain.batch_download(contexts=contexts, no_exists=no_exists,
                                                                                 username="豆瓣想看")
                            if downloads and not lefts:
                                # 全部下载完成
                                logger.info(f'{mediainfo.title_year} 下载完成')
                                action = "download"
                            else:
                                # 未完成下载
                                logger.info(f'{mediainfo.title_year} 未下载未完整，添加订阅 ...')
                                # 添加订阅
                                self.subscribechain.add(title=mediainfo.title,
                                                        year=mediainfo.year,
                                                        mtype=mediainfo.type,
                                                        tmdbid=mediainfo.tmdb_id,
                                                        season=meta.begin_season,
                                                        exist_ok=True,
                                                        username="豆瓣想看")
                                action = "subscribe"
                    # 存储历史记录
                    history.append({
                        "action": action,
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": mediainfo.tmdb_id,
                        "doubanid": douban_id,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                except Exception as err:
                    logger.error(f'同步用户 {user_id} 豆瓣想看数据出错：{str(err)}')
            logger.info(f"用户 {user_id} 豆瓣想看同步完成")
        # 保存历史记录
        self.save_data('history', history)
        # 缓存只清理一次
        self._clearflag = False

    @eventmanager.register(EventType.DoubanSync)
    def remote_sync(self, event: Event):
        """
        豆瓣想看同步
        """
        if event:
            logger.info("收到命令，开始执行豆瓣想看同步 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始同步豆瓣想看 ...",
                              userid=event.event_data.get("user"))
        self.sync()

        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="同步豆瓣想看数据完成！", userid=event.event_data.get("user"))
