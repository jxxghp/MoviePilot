from datetime import datetime, timedelta
from functools import reduce
from pathlib import Path
from threading import RLock
from typing import Optional, Any, List, Dict, Tuple
from xml.dom.minidom import parseString

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests import Response

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo
from app.schemas.types import MediaType, EventType
from app.utils.http import RequestUtils

lock = RLock()


class BestFilmVersion(_PluginBase):
    # 插件名称
    plugin_name = "收藏洗版"
    # 插件描述
    plugin_desc = "Jellyfin/Emby/Plex点击收藏电影后，自动订阅洗版。"
    # 插件图标
    plugin_icon = "like.jpg"
    # 主题色
    plugin_color = "#E4003F"
    # 插件版本
    plugin_version = "2.0"
    # 插件作者
    plugin_author = "wlj"
    # 作者主页
    author_url = "https://github.com/developer-wlj"
    # 插件配置项ID前缀
    plugin_config_prefix = "bestfilmversion_"
    # 加载顺序
    plugin_order = 13
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None
    subscribechain = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _notify: bool = False
    _webhook_enabled: bool = False
    _only_once: bool = False

    def init_plugin(self, config: dict = None):
        self._cache_path = settings.TEMP_PATH / "__best_film_version_cache__"
        self.subscribechain = SubscribeChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._webhook_enabled = config.get("webhook_enabled")
            self._only_once = config.get("only_once")

        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if not self._webhook_enabled:
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.sync,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="收藏洗版")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{str(err)}")
                else:
                    self._scheduler.add_job(self.sync, "interval", minutes=30, name="收藏洗版")

            if self._only_once:
                self._only_once = False
                self.update_config({
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "notify": self._notify,
                    "webhook_enabled": self._webhook_enabled,
                    "only_once": self._only_once
                })
                self._scheduler.add_job(self.sync, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="立即运行收藏洗版")
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
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
                                    'md': 3
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
                                    'md': 3
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'only_once',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'webhook_enabled',
                                            'label': 'Webhook',
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
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '支持主动定时获取媒体库数据和Webhook实时触发两种方式，两者只能选其一，'
                                                    'Webhook需要在媒体服务器设置发送Webhook报文。'
                                                    'Plex使用主动获取时，建议执行周期设置大于1小时，'
                                                    '收藏Api调用Plex官网接口，有频率限制。'
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
            "notify": False,
            "cron": "*/30 * * * *",
            "webhook_enabled": False,
            "only_once": False
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
            tmdbid = history.get("tmdbid")
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
                                                        'href': f"https://www.themoviedb.org/movie/{tmdbid}",
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
        通过流媒体管理工具收藏,自动洗版
        """
        # 获取锁
        _is_lock: bool = lock.acquire(timeout=60)
        if not _is_lock:
            return
        try:
            # 读取缓存
            caches = self._cache_path.read_text().split("\n") if self._cache_path.exists() else []
            # 读取历史记录
            history = self.get_data('history') or []

            # 媒体服务器类型，多个以,分隔
            if not settings.MEDIASERVER:
                return
            media_servers = settings.MEDIASERVER.split(',')

            # 读取收藏
            all_items = {}
            for media_server in media_servers:
                if media_server == 'jellyfin':
                    all_items['jellyfin'] = self.jellyfin_get_items()
                elif media_server == 'emby':
                    all_items['emby'] = self.emby_get_items()
                else:
                    all_items['plex'] = self.plex_get_watchlist()

            def function(y, x):
                return y if (x['Name'] in [i['Name'] for i in y]) else (lambda z, u: (z.append(u), z))(y, x)[1]

            # 处理所有结果
            for server, all_item in all_items.items():
                # all_item 根据电影名去重
                result = reduce(function, all_item, [])
                for data in result:
                    # 检查缓存
                    if data.get('Name') in caches:
                        continue

                    # 获取详情
                    if server == 'jellyfin':
                        item_info_resp = Jellyfin().get_iteminfo(itemid=data.get('Id'))
                    elif server == 'emby':
                        item_info_resp = Emby().get_iteminfo(itemid=data.get('Id'))
                    else:
                        item_info_resp = self.plex_get_iteminfo(itemid=data.get('Id'))
                    logger.debug(f'BestFilmVersion插件 item打印 {item_info_resp}')
                    if not item_info_resp:
                        continue

                    # 只接受Movie类型
                    if data.get('Type') != 'Movie':
                        continue

                    # 获取tmdb_id
                    tmdb_id = item_info_resp.tmdbid
                    if not tmdb_id:
                        continue
                    # 识别媒体信息
                    mediainfo: MediaInfo = self.chain.recognize_media(tmdbid=tmdb_id, mtype=MediaType.MOVIE)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{data.get("Name")}，tmdbid：{tmdb_id}')
                        continue
                    # 添加订阅
                    self.subscribechain.add(mtype=MediaType.MOVIE,
                                            title=mediainfo.title,
                                            year=mediainfo.year,
                                            tmdbid=mediainfo.tmdb_id,
                                            best_version=True,
                                            username="收藏洗版",
                                            exist_ok=True)
                    # 加入缓存
                    caches.append(data.get('Name'))
                    # 存储历史记录
                    if mediainfo.tmdb_id not in [h.get("tmdbid") for h in history]:
                        history.append({
                            "title": mediainfo.title,
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
            # 保存历史记录
            self.save_data('history', history)
            # 保存缓存
            self._cache_path.write_text("\n".join(caches))
        finally:
            lock.release()

    def jellyfin_get_items(self) -> List[dict]:
        # 获取所有user
        users_url = "[HOST]Users?&apikey=[APIKEY]"
        users = self.get_users(Jellyfin().get_data(users_url))
        if not users:
            logger.info(f"bestfilmversion/users_url: {users_url}")
            return []
        all_items = []
        for user in users:
            # 根据加入日期 降序排序
            url = "[HOST]Users/" + user + "/Items?SortBy=DateCreated%2CSortName" \
                                          "&SortOrder=Descending" \
                                          "&Filters=IsFavorite" \
                                          "&Recursive=true" \
                                          "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo" \
                                          "&CollapseBoxSetItems=false" \
                                          "&ExcludeLocationTypes=Virtual" \
                                          "&EnableTotalRecordCount=false" \
                                          "&Limit=20" \
                                          "&apikey=[APIKEY]"
            resp = self.get_items(Jellyfin().get_data(url))
            if not resp:
                continue
            all_items.extend(resp)
        return all_items

    def emby_get_items(self) -> List[dict]:
        # 获取所有user
        get_users_url = "[HOST]Users?&api_key=[APIKEY]"
        users = self.get_users(Emby().get_data(get_users_url))
        if not users:
            return []
        all_items = []
        for user in users:
            # 根据加入日期 降序排序
            url = "[HOST]emby/Users/" + user + "/Items?SortBy=DateCreated%2CSortName" \
                                               "&SortOrder=Descending" \
                                               "&Filters=IsFavorite" \
                                               "&Recursive=true" \
                                               "&Fields=PrimaryImageAspectRatio%2CBasicSyncInfo" \
                                               "&CollapseBoxSetItems=false" \
                                               "&ExcludeLocationTypes=Virtual" \
                                               "&EnableTotalRecordCount=false" \
                                               "&Limit=20&api_key=[APIKEY]"
            resp = self.get_items(Emby().get_data(url))
            if not resp:
                continue
            all_items.extend(resp)
        return all_items

    @staticmethod
    def get_items(resp: Response):
        try:
            if resp:
                return resp.json().get("Items") or []
            else:
                return []
        except Exception as e:
            print(str(e))
            return []

    @staticmethod
    def get_users(resp: Response):
        try:
            if resp:
                return [data['Id'] for data in resp.json()]
            else:
                logger.error(f"BestFilmVersion/Users 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接BestFilmVersion/Users 出错：" + str(e))
            return []

    @staticmethod
    def plex_get_watchlist() -> List[dict]:
        # 根据加入日期 降序排序
        url = f"https://metadata.provider.plex.tv/library/sections/watchlist/all?type=1&sort=addedAt%3Adesc" \
              f"&X-Plex-Container-Start=0&X-Plex-Container-Size=50" \
              f"&X-Plex-Token={settings.PLEX_TOKEN}"
        res = []
        try:
            resp = RequestUtils().get_res(url=url)
            if resp:
                dom = parseString(resp.text)
                # 获取文档元素对象
                elem = dom.documentElement
                # 获取 指定元素
                eles = elem.getElementsByTagName('Video')
                if not eles:
                    return []
                for ele in eles:
                    data = {}
                    # 获取标签中内容
                    ele_id = ele.attributes['ratingKey'].nodeValue
                    ele_title = ele.attributes['title'].nodeValue
                    ele_type = ele.attributes['type'].nodeValue
                    _type = "Movie" if ele_type == "movie" else ""
                    data['Id'] = ele_id
                    data['Name'] = ele_title
                    data['Type'] = _type
                    res.append(data)
                return res
            else:
                logger.error(f"Plex/Watchlist 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Plex/Watchlist 出错：" + str(e))
            return []

    @staticmethod
    def plex_get_iteminfo(itemid):
        url = f"https://metadata.provider.plex.tv/library/metadata/{itemid}" \
              f"?X-Plex-Token={settings.PLEX_TOKEN}"
        ids = []
        try:
            resp = RequestUtils(accept_type="application/json, text/plain, */*").get_res(url=url)
            if resp:
                metadata = resp.json().get('MediaContainer').get('Metadata')
                for item in metadata:
                    _guid = item.get('Guid')
                    if not _guid:
                        continue

                    id_list = [h.get('id') for h in _guid if h.get('id').__contains__("tmdb")]
                    if not id_list:
                        continue

                    ids.append({'Name': 'TheMovieDb', 'Url': id_list[0]})

                if not ids:
                    return []
                return {'ExternalUrls': ids}
            else:
                logger.error(f"Plex/Items 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Plex/Items 出错：" + str(e))
            return []

    @eventmanager.register(EventType.WebhookMessage)
    def webhook_message_action(self, event):

        if not self._enabled:
            return
        if not self._webhook_enabled:
            return

        data: WebhookEventInfo = event.event_data
        # 排除不是收藏调用
        if data.channel not in ['jellyfin', 'emby', 'plex']:
            return
        if data.channel in ['emby', 'plex'] and data.event != 'item.rate':
            return
        if data.channel == 'jellyfin' and data.save_reason != 'UpdateUserRating':
            return
        logger.info(f'BestFilmVersion/webhook_message_action WebhookEventInfo打印：{data}')

        # 获取锁
        _is_lock: bool = lock.acquire(timeout=60)
        if not _is_lock:
            return
        try:
            if not data.tmdb_id:
                info = None
                if (data.channel == 'jellyfin'
                        and data.save_reason == 'UpdateUserRating'
                        and data.item_favorite):
                    info = Jellyfin().get_iteminfo(itemid=data.item_id)
                elif data.channel == 'emby' and data.event == 'item.rate':
                    info = Emby().get_iteminfo(itemid=data.item_id)
                elif data.channel == 'plex' and data.event == 'item.rate':
                    info = Plex().get_iteminfo(itemid=data.item_id)
                logger.debug(f'BestFilmVersion/webhook_message_action item打印：{info}')
                if not info:
                    return
                if info.item_type not in ['Movie', 'MOV', 'movie']:
                    return
                # 获取tmdb_id
                tmdb_id = info.tmdbid
            else:
                tmdb_id = data.tmdb_id
                if (data.channel == 'jellyfin'
                        and (data.save_reason != 'UpdateUserRating' or not data.item_favorite)):
                    return
                if data.item_type not in ['Movie', 'MOV', 'movie']:
                    return
            # 识别媒体信息
            mediainfo = self.chain.recognize_media(tmdbid=tmdb_id, mtype=MediaType.MOVIE)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{data.item_name}，tmdbID：{tmdb_id}')
                return
            # 读取缓存
            caches = self._cache_path.read_text().split("\n") if self._cache_path.exists() else []
            # 检查缓存
            if data.item_name in caches:
                return
            # 读取历史记录
            history = self.get_data('history') or []
            # 添加订阅
            self.subscribechain.add(mtype=MediaType.MOVIE,
                                    title=mediainfo.title,
                                    year=mediainfo.year,
                                    tmdbid=mediainfo.tmdb_id,
                                    best_version=True,
                                    username="收藏洗版",
                                    exist_ok=True)
            # 加入缓存
            caches.append(data.item_name)
            # 存储历史记录
            if mediainfo.tmdb_id not in [h.get("tmdbid") for h in history]:
                history.append({
                    "title": mediainfo.title,
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "tmdbid": mediainfo.tmdb_id,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            # 保存历史记录
            self.save_data('history', history)
            # 保存缓存
            self._cache_path.write_text("\n".join(caches))
        finally:
            lock.release()
