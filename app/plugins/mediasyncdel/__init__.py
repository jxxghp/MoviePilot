import datetime
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.qbittorrent import Qbittorrent
from app.modules.themoviedb.tmdbv3api import Episode
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas.types import NotificationType, EventType, MediaType
from app.utils.path_utils import PathUtils


class MediaSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "媒体库同步删除"
    # 插件描述
    plugin_desc = "媒体库删除媒体后同步删除历史记录、源文件和下载任务。"
    # 插件图标
    plugin_icon = "mediasyncdel.png"
    # 主题色
    plugin_color = "#ff1a1a"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediasyncdel_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    episode = None
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled = False
    _sync_type: str = ""
    _cron: str = ""
    _notify = False
    _del_source = False
    _exclude_path = None
    _transferhis = None
    _downloadhis = None
    qb = None
    tr = None

    def init_plugin(self, config: dict = None):
        self._transferhis = TransferHistoryOper(self.db)
        self._downloadhis = DownloadHistoryOper(self.db)
        self.episode = Episode()
        self.qb = Qbittorrent()
        self.tr = Transmission()

        # 停止现有任务
        self.stop_service()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._sync_type = config.get("sync_type")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._del_source = config.get("del_source")
            self._exclude_path = config.get("exclude_path")

        if self._enabled and str(self._sync_type) == "log":
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.sync_del_by_log,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="媒体库同步删除")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")
            else:
                self._scheduler.add_job(self.sync_del_by_log, "interval", minutes=30, name="媒体库同步删除")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
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
                                            'model': 'del_source',
                                            'label': '删除源文件',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'sync_type',
                                            'label': '同步方式',
                                            'items': [
                                                {'title': 'webhook', 'value': 'webhook'},
                                                {'title': '日志', 'value': 'log'},
                                                {'title': 'Scripter X', 'value': 'plugin'}
                                            ]
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'exclude_path',
                                            'label': '排除路径'
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
                                            'text': '同步方式分为webhook、日志同步和Scripter X。'
                                                    'webhook需要Emby4.8.0.45及以上开启媒体删除的webhook'
                                                    '（建议使用媒体库刮削插件覆盖元数据重新刮削剧集路径）。'
                                                    '日志同步需要配置执行周期，默认30分钟执行一次。'
                                                    'Scripter X方式需要emby安装并配置Scripter X插件，无需配置执行周期。'
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
            "del_source": False,
            "sync_type": "webhook",
            "cron": "*/30 * * * *",
            "exclude_path": "",
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
        historys = sorted(historys, key=lambda x: x.get('del_time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            htype = history.get("type")
            title = history.get("title")
            year = history.get("year")
            season = history.get("season")
            episode = history.get("episode")
            image = history.get("image")
            del_time = history.get("del_time")

            if season:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'年份：{year}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'季：{season}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'集：{episode}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]
            else:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'年份：{year}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]

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
                                                'src': image,
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
                                    'content': sub_contents
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

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del_by_webhook(self, event: Event):
        """
        emby删除媒体库同步删除历史记录
        webhook
        """
        if not self._enabled or str(self._sync_type) != "webhook":
            return

        event_data = event.event_data
        event_type = event_data.event

        # Emby Webhook event_type = library.deleted
        if not event_type or str(event_type) != 'library.deleted':
            return

        # 媒体类型
        media_type = event_data.item_type
        # 媒体名称
        media_name = event_data.item_name
        # 媒体路径
        media_path = event_data.item_path
        # tmdb_id
        tmdb_id = event_data.tmdb_id
        # 季数
        season_num = event_data.season_id
        # 集数
        episode_num = event_data.episode_id

        self.__sync_del(media_type=media_type,
                        media_name=media_name,
                        media_path=media_path,
                        tmdb_id=tmdb_id,
                        season_num=season_num,
                        episode_num=episode_num)

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del_by_plugin(self, event):
        """
        emby删除媒体库同步删除历史记录
        Scripter X插件
        """
        if not self._enabled or str(self._sync_type) != "plugin":
            return

        event_data = event.event_data
        event_type = event_data.event

        # Scripter X插件 event_type = media_del
        if not event_type or str(event_type) != 'media_del':
            return

        # Scripter X插件 需要是否虚拟标识
        item_isvirtual = event_data.item_isvirtual
        if not item_isvirtual:
            logger.error("Scripter X插件方式，item_isvirtual参数未配置，为防止误删除，暂停插件运行")
            self.update_config({
                "enabled": False,
                "del_source": self._del_source,
                "exclude_path": self._exclude_path,
                "notify": self._notify,
                "cron": self._cron,
                "sync_type": self._sync_type,
            })
            return

        # 如果是虚拟item，则直接return，不进行删除
        if item_isvirtual == 'True':
            return

        # 媒体类型
        media_type = event_data.item_type
        # 媒体名称
        media_name = event_data.item_name
        # 媒体路径
        media_path = event_data.item_path
        # tmdb_id
        tmdb_id = event_data.tmdb_id
        # 季数
        season_num = event_data.season_id
        # 集数
        episode_num = event_data.episode_id

        self.__sync_del(media_type=media_type,
                        media_name=media_name,
                        media_path=media_path,
                        tmdb_id=tmdb_id,
                        season_num=season_num,
                        episode_num=episode_num)

    def __sync_del(self, media_type: str, media_name: str, media_path: str,
                   tmdb_id: int, season_num: int, episode_num: int):
        """
        执行删除逻辑
        """
        if not media_type:
            logger.error(f"{media_name} 同步删除失败，未获取到媒体类型")
            return
        if not tmdb_id or not str(tmdb_id).isdigit():
            logger.error(f"{media_name} 同步删除失败，未获取到TMDB ID")
            return

        if self._exclude_path and media_path and any(
                os.path.abspath(media_path).startswith(os.path.abspath(path)) for path in
                self._exclude_path.split(",")):
            logger.info(f"媒体路径 {media_path} 已被排除，暂不处理")
            return

        # 查询转移记录
        msg, transfer_history = self.__get_transfer_his(media_type=media_type,
                                                        media_name=media_name,
                                                        tmdb_id=tmdb_id,
                                                        season_num=season_num,
                                                        episode_num=episode_num)

        logger.info(f"正在同步删除{msg}")

        if not transfer_history:
            logger.warn(f"{media_type} {media_name} 未获取到可删除数据，可使用媒体库刮削插件覆盖所有元数据")
            return

        # 开始删除
        image = 'https://emby.media/notificationicon.png'
        year = None
        del_cnt = 0
        stop_cnt = 0
        error_cnt = 0
        for transferhis in transfer_history:
            title = transferhis.title
            if title not in media_name:
                logger.warn(
                    f"当前转移记录 {transferhis.id} {title} {transferhis.tmdbid} 与删除媒体{media_name}不符，防误删，暂不自动删除")
                continue
            image = transferhis.image
            year = transferhis.year

            # 0、删除转移记录
            self._transferhis.delete(transferhis.id)

            # 删除种子任务
            if self._del_source:
                # 1、直接删除源文件
                if transferhis.src and Path(transferhis.src).suffix in settings.RMT_MEDIAEXT:
                    source_name = os.path.basename(transferhis.src)
                    source_path = str(transferhis.src).replace(source_name, "")
                    self.delete_media_file(filedir=source_path,
                                           filename=source_name)
                    if transferhis.download_hash:
                        try:
                            # 2、判断种子是否被删除完
                            delete_flag, success_flag, handle_cnt = self.handle_torrent(src=transferhis.src,
                                                                                        torrent_hash=transferhis.download_hash)
                            if not success_flag:
                                error_cnt += 1
                            else:
                                if delete_flag:
                                    del_cnt += handle_cnt
                                else:
                                    stop_cnt += handle_cnt
                        except Exception as e:
                            logger.error("删除种子失败，尝试删除源文件：%s" % str(e))

        logger.info(f"同步删除 {msg} 完成！")

        # 发送消息
        if self._notify:
            if media_type == "Episode":
                # 根据tmdbid获取图片
                images = self.episode.images(tv_id=tmdb_id,
                                             season_num=season_num,
                                             episode_num=episode_num)
                if images:
                    image = self.get_tmdbimage_url(images[-1].get("file_path"), prefix="original")

            torrent_cnt_msg = ""
            if del_cnt:
                torrent_cnt_msg += f"删除种子{del_cnt}个\n"
            if stop_cnt:
                torrent_cnt_msg += f"暂停种子{stop_cnt}个\n"
            if error_cnt:
                torrent_cnt_msg += f"删种失败{error_cnt}个\n"
            # 发送通知
            self.post_message(
                mtype=NotificationType.MediaServer,
                title="媒体库同步删除任务完成",
                image=image,
                text=f"{msg}\n"
                     f"删除记录{len(transfer_history)}个\n"
                     f"{torrent_cnt_msg}"
                     f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"
            )

        # 读取历史记录
        history = self.get_data('history') or []

        history.append({
            "type": "电影" if media_type == "Movie" or media_type == "MOV" else "电视剧",
            "title": media_name,
            "year": year,
            "path": media_path,
            "season": season_num,
            "episode": episode_num,
            "image": image,
            "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        })

        # 保存历史
        self.save_data("history", history)

    def __get_transfer_his(self, media_type: str, media_name: str,
                           tmdb_id: int, season_num: int, episode_num: int):
        """
        查询转移记录
        """

        # 季数
        if season_num:
            season_num = str(season_num).rjust(2, '0')
        # 集数
        if episode_num:
            episode_num = str(episode_num).rjust(2, '0')

        # 类型
        mtype = MediaType.MOVIE if media_type in ["Movie", "MOV"] else MediaType.TV

        # 删除电影
        if mtype == MediaType.MOVIE:
            msg = f'电影 {media_name} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id,
                                                                               mtype=mtype.value)
        # 删除电视剧
        elif mtype == MediaType.TV and not season_num and not episode_num:
            msg = f'剧集 {media_name} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id,
                                                                               mtype=mtype.value)
        # 删除季 S02
        elif mtype == MediaType.TV and season_num and not episode_num:
            if not season_num or not str(season_num).isdigit():
                logger.error(f"{media_name} 季同步删除失败，未获取到具体季")
                return
            msg = f'剧集 {media_name} S{season_num} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id,
                                                                               mtype=mtype.value,
                                                                               season=f'S{season_num}')
        # 删除剧集S02E02
        elif mtype == MediaType.TV and season_num and episode_num:
            if not season_num or not str(season_num).isdigit() or not episode_num or not str(episode_num).isdigit():
                logger.error(f"{media_name} 集同步删除失败，未获取到具体集")
                return
            msg = f'剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id,
                                                                               mtype=mtype.value,
                                                                               season=f'S{season_num}',
                                                                               episode=f'E{episode_num}')
        else:
            return "", []

        return msg, transfer_history

    def sync_del_by_log(self):
        """
        emby删除媒体库同步删除历史记录
        日志方式
        """
        # 读取历史记录
        history = self.get_data('history') or []

        # 媒体服务器类型
        media_server = settings.MEDIASERVER

        last_time = self.get_data("last_time")
        del_medias = []
        if media_server == 'emby':
            del_medias = self.parse_emby_log(last_time)
        elif media_server == 'jellyfin':
            del_medias = self.parse_jellyfin_log(last_time)
        elif media_server == 'plex':
            # TODO plex解析日志
            return

        if not del_medias:
            logger.error("未解析到已删除媒体信息")
            return

        # 遍历删除
        for del_media in del_medias:
            # 媒体类型 Movie|Series|Season|Episode
            media_type = del_media.get("type")
            # 媒体名称 蜀山战纪
            media_name = del_media.get("name")
            # 媒体年份 2015
            media_year = del_media.get("year")
            # 媒体路径 /data/series/国产剧/蜀山战纪 (2015)/Season 2/蜀山战纪 - S02E01 - 第1集.mp4
            media_path = del_media.get("path")
            # 季数 S02
            media_season = del_media.get("season")
            # 集数 E02
            media_episode = del_media.get("episode")

            # 排除路径不处理
            if self._exclude_path and media_path and any(
                    os.path.abspath(media_path).startswith(os.path.abspath(path)) for path in
                    self._exclude_path.split(",")):
                logger.info(f"媒体路径 {media_path} 已被排除，暂不处理")
                return

            # 获取删除的记录
            # 删除电影
            if media_type == "Movie":
                msg = f'电影 {media_name}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    title=media_name,
                    year=media_year)
            # 删除电视剧
            elif media_type == "Series":
                msg = f'剧集 {media_name}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    title=media_name,
                    year=media_year)
            # 删除季 S02
            elif media_type == "Season":
                msg = f'剧集 {media_name} {media_season}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    title=media_name,
                    year=media_year,
                    season=media_season)
            # 删除剧集S02E02
            elif media_type == "Episode":
                msg = f'剧集 {media_name} {media_season}{media_episode}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    title=media_name,
                    year=media_year,
                    season=media_season,
                    episode=media_episode)
            else:
                continue

            logger.info(f"正在同步删除 {msg}")

            if not transfer_history:
                logger.info(f"未获取到 {msg} 转移记录")
                continue

            logger.info(f"获取到删除历史记录数量 {len(transfer_history)}")

            # 开始删除
            image = 'https://emby.media/notificationicon.png'
            del_cnt = 0
            stop_cnt = 0
            error_cnt = 0
            for transferhis in transfer_history:
                title = transferhis.title
                if title not in media_name:
                    logger.warn(
                        f"当前转移记录 {transferhis.id} {title} {transferhis.tmdbid} 与删除媒体{media_name}不符，防误删，暂不自动删除")
                    continue
                image = transferhis.image
                # 0、删除转移记录
                self._transferhis.delete(transferhis.id)

                # 删除种子任务
                if self._del_source:
                    # 1、直接删除源文件
                    if transferhis.src and Path(transferhis.src).suffix in settings.RMT_MEDIAEXT:
                        source_name = os.path.basename(transferhis.src)
                        source_path = str(transferhis.src).replace(source_name, "")
                        self.delete_media_file(filedir=source_path,
                                               filename=source_name)
                        if transferhis.download_hash:
                            try:
                                # 2、判断种子是否被删除完
                                delete_flag, success_flag, handle_cnt = self.handle_torrent(src=transferhis.src,
                                                                                            torrent_hash=transferhis.download_hash)
                                if not success_flag:
                                    error_cnt += 1
                                else:
                                    if delete_flag:
                                        del_cnt += handle_cnt
                                    else:
                                        stop_cnt += handle_cnt
                            except Exception as e:
                                logger.error("删除种子失败，尝试删除源文件：%s" % str(e))

            logger.info(f"同步删除 {msg} 完成！")

            # 发送消息
            if self._notify:
                torrent_cnt_msg = ""
                if del_cnt:
                    torrent_cnt_msg += f"删除种子{del_cnt}个\n"
                if stop_cnt:
                    torrent_cnt_msg += f"暂停种子{stop_cnt}个\n"
                if error_cnt:
                    torrent_cnt_msg += f"删种失败{error_cnt}个\n"
                self.post_message(
                    mtype=NotificationType.MediaServer,
                    title="媒体库同步删除任务完成",
                    text=f"{msg}\n"
                         f"删除记录{len(transfer_history)}个\n"
                         f"{torrent_cnt_msg}"
                         f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}",
                    image=image)

            history.append({
                "type": "电影" if media_type == "Movie" else "电视剧",
                "title": media_name,
                "year": media_year,
                "path": media_path,
                "season": media_season,
                "episode": media_episode,
                "image": image,
                "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
            })

        # 保存历史
        self.save_data("history", history)

        self.save_data("last_time", datetime.datetime.now())

    def handle_torrent(self, src: str, torrent_hash: str):
        """
        判断种子是否局部删除
        局部删除则暂停种子
        全部删除则删除种子
        """
        download_id = torrent_hash
        download = settings.DOWNLOADER
        history_key = "%s-%s" % (download, torrent_hash)
        plugin_id = "TorrentTransfer"
        transfer_history = self.get_data(key=history_key,
                                         plugin_id=plugin_id)
        logger.info(f"查询到 {history_key} 转种历史 {transfer_history}")

        handle_cnt = 0
        try:
            # 删除本次种子记录
            self._downloadhis.delete_file_by_fullpath(fullpath=src)

            # 根据种子hash查询所有下载器文件记录
            download_files = self._downloadhis.get_files_by_hash(download_hash=torrent_hash)
            if not download_files:
                logger.error(
                    f"未查询到种子任务 {torrent_hash} 存在文件记录，未执行下载器文件同步或该种子已被删除")
                return False, False, 0

            # 查询未删除数
            no_del_cnt = 0
            for download_file in download_files:
                if download_file and download_file.state and int(download_file.state) == 1:
                    no_del_cnt += 1

            if no_del_cnt > 0:
                logger.info(
                    f"查询种子任务 {torrent_hash} 存在 {no_del_cnt} 个未删除文件，执行暂停种子操作")
                delete_flag = False
            else:
                logger.info(
                    f"查询种子任务 {torrent_hash} 文件已全部删除，执行删除种子操作")
                delete_flag = True

            # 如果有转种记录，则删除转种后的下载任务
            if transfer_history and isinstance(transfer_history, dict):
                download = transfer_history['to_download']
                download_id = transfer_history['to_download_id']
                delete_source = transfer_history['delete_source']

                # 删除种子
                if delete_flag:
                    # 删除转种记录
                    self.del_data(key=history_key, plugin_id=plugin_id)

                    # 转种后未删除源种时，同步删除源种
                    if not delete_source:
                        logger.info(f"{history_key} 转种时未删除源下载任务，开始删除源下载任务…")

                        # 删除源种子
                        logger.info(f"删除源下载器下载任务：{settings.DOWNLOADER} - {torrent_hash}")
                        self.chain.remove_torrents(torrent_hash)
                        handle_cnt += 1

                    # 删除转种后任务
                    logger.info(f"删除转种后下载任务：{download} - {download_id}")
                    # 删除转种后下载任务
                    if download == "transmission":
                        self.tr.delete_torrents(delete_file=True,
                                                ids=download_id)
                    else:
                        self.qb.delete_torrents(delete_file=True,
                                                ids=download_id)
                    handle_cnt += 1
                else:
                    # 暂停种子
                    # 转种后未删除源种时，同步暂停源种
                    if not delete_source:
                        logger.info(f"{history_key} 转种时未删除源下载任务，开始暂停源下载任务…")

                        # 暂停源种子
                        logger.info(f"暂停源下载器下载任务：{settings.DOWNLOADER} - {torrent_hash}")
                        self.chain.stop_torrents(torrent_hash)
                        handle_cnt += 1

            else:
                # 未转种de情况
                if delete_flag:
                    # 删除源种子
                    logger.info(f"删除源下载器下载任务：{download} - {download_id}")
                    self.chain.remove_torrents(download_id)
                else:
                    # 暂停源种子
                    logger.info(f"暂停源下载器下载任务：{download} - {download_id}")
                    self.chain.stop_torrents(download_id)
                handle_cnt += 1

            # 处理辅种
            handle_cnt = self.__del_seed(download=download,
                                         download_id=download_id,
                                         action_flag="del" if delete_flag else 'stop',
                                         handle_cnt=handle_cnt)

            return delete_flag, True, handle_cnt
        except Exception as e:
            logger.error(f"删种失败： {e}")
            return False, False, 0

    def __del_seed(self, download, download_id, action_flag, handle_cnt):
        """
        删除辅种
        """
        # 查询是否有辅种记录
        history_key = download_id
        plugin_id = "IYUUAutoSeed"
        seed_history = self.get_data(key=history_key,
                                     plugin_id=plugin_id) or []
        logger.info(f"查询到 {history_key} 辅种历史 {seed_history}")

        # 有辅种记录则处理辅种
        if seed_history and isinstance(seed_history, list):
            for history in seed_history:
                downloader = history['downloader']
                torrents = history['torrents']
                if not downloader or not torrents:
                    return
                if not isinstance(torrents, list):
                    torrents = [torrents]

                # 删除辅种历史中与本下载器相同的辅种记录
                if str(downloader) == str(download):
                    for torrent in torrents:
                        handle_cnt += 1
                        if str(download) == "qbittorrent":
                            # 删除辅种
                            if action_flag == "del":
                                logger.info(f"删除辅种：{downloader} - {torrent}")
                                self.qb.delete_torrents(delete_file=True,
                                                        ids=torrent)
                            # 暂停辅种
                            if action_flag == "stop":
                                self.qb.stop_torrents(torrent)
                                logger.info(f"辅种：{downloader} - {torrent} 暂停")
                        else:
                            # 删除辅种
                            if action_flag == "del":
                                logger.info(f"删除辅种：{downloader} - {torrent}")
                                self.tr.delete_torrents(delete_file=True,
                                                        ids=torrent)
                            # 暂停辅种
                            if action_flag == "stop":
                                self.tr.stop_torrents(torrent)
                                logger.info(f"辅种：{downloader} - {torrent} 暂停")
                    # 删除本下载器辅种历史
                    if action_flag == "del":
                        del history
                    break

            # 更新辅种历史
            self.save_data(key=history_key,
                           value=seed_history,
                           plugin_id=plugin_id)

        return handle_cnt

    @staticmethod
    def parse_emby_log(last_time):
        log_url = "{HOST}System/Logs/embyserver.txt?api_key={APIKEY}"
        log_res = Emby().get_data(log_url)
        if not log_res or log_res.status_code != 200:
            logger.error("获取emby日志失败，请检查服务器配置")
            return []

        # 正则解析删除的媒体信息
        pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{3}) Info App: Removing item from database, Type: (\w+), Name: (.*), Path: (.*), Id: (\d+)'
        matches = re.findall(pattern, log_res.text)

        del_medias = []
        # 循环获取媒体信息
        for match in matches:
            mtime = match[0]
            # 排除已处理的媒体信息
            if last_time and mtime < last_time:
                continue

            mtype = match[1]
            name = match[2]
            path = match[3]

            year = None
            year_pattern = r'\(\d+\)'
            year_match = re.search(year_pattern, path)
            if year_match:
                year = year_match.group()[1:-1]

            season = None
            episode = None
            if mtype == 'Episode' or mtype == 'Season':
                name_pattern = r"\/([\u4e00-\u9fa5]+)(?= \()"
                season_pattern = r"Season\s*(\d+)"
                episode_pattern = r"S\d+E(\d+)"
                name_match = re.search(name_pattern, path)
                season_match = re.search(season_pattern, path)
                episode_match = re.search(episode_pattern, path)

                if name_match:
                    name = name_match.group(1)

                if season_match:
                    season = season_match.group(1)
                    if int(season) < 10:
                        season = f'S0{season}'
                    else:
                        season = f'S{season}'
                else:
                    season = None

                if episode_match:
                    episode = episode_match.group(1)
                    episode = f'E{episode}'
                else:
                    episode = None

            media = {
                "time": mtime,
                "type": mtype,
                "name": name,
                "year": year,
                "path": path,
                "season": season,
                "episode": episode,
            }
            logger.debug(f"解析到删除媒体：{json.dumps(media)}")
            del_medias.append(media)

        return del_medias

    @staticmethod
    def parse_jellyfin_log(last_time: datetime):
        # 根据加入日期 降序排序
        log_url = "{HOST}System/Logs/Log?name=log_%s.log&api_key={APIKEY}" % datetime.date.today().strftime("%Y%m%d")
        log_res = Jellyfin().get_data(log_url)
        if not log_res or log_res.status_code != 200:
            logger.error("获取jellyfin日志失败，请检查服务器配置")
            return []

        # 正则解析删除的媒体信息
        pattern = r'\[(.*?)\].*?Removing item, Type: "(.*?)", Name: "(.*?)", Path: "(.*?)"'
        matches = re.findall(pattern, log_res.text)

        del_medias = []
        # 循环获取媒体信息
        for match in matches:
            mtime = match[0]
            # 排除已处理的媒体信息
            if last_time and mtime < last_time:
                continue

            mtype = match[1]
            name = match[2]
            path = match[3]

            year = None
            year_pattern = r'\(\d+\)'
            year_match = re.search(year_pattern, path)
            if year_match:
                year = year_match.group()[1:-1]

            season = None
            episode = None
            if mtype == 'Episode' or mtype == 'Season':
                name_pattern = r"\/([\u4e00-\u9fa5]+)(?= \()"
                season_pattern = r"Season\s*(\d+)"
                episode_pattern = r"S\d+E(\d+)"
                name_match = re.search(name_pattern, path)
                season_match = re.search(season_pattern, path)
                episode_match = re.search(episode_pattern, path)

                if name_match:
                    name = name_match.group(1)

                if season_match:
                    season = season_match.group(1)
                    if int(season) < 10:
                        season = f'S0{season}'
                    else:
                        season = f'S{season}'
                else:
                    season = None

                if episode_match:
                    episode = episode_match.group(1)
                    episode = f'E{episode}'
                else:
                    episode = None

            media = {
                "time": mtime,
                "type": mtype,
                "name": name,
                "year": year,
                "path": path,
                "season": season,
                "episode": episode,
            }
            logger.debug(f"解析到删除媒体：{json.dumps(media)}")
            del_medias.append(media)

        return del_medias

    @staticmethod
    def delete_media_file(filedir: str, filename: str):
        """
        删除媒体文件，空目录也会被删除
        """
        filedir = os.path.normpath(filedir).replace("\\", "/")
        file = os.path.join(filedir, filename)
        try:
            if not os.path.exists(file):
                return False, f"{file} 不存在"
            os.remove(file)
            nfoname = f"{os.path.splitext(filename)[0]}.nfo"
            nfofile = os.path.join(filedir, nfoname)
            if os.path.exists(nfofile):
                os.remove(nfofile)
            # 检查空目录并删除
            if re.findall(r"^S\d{2}|^Season", os.path.basename(filedir), re.I):
                # 当前是季文件夹，判断并删除
                seaon_dir = filedir
                if seaon_dir.count('/') > 1 and not PathUtils.get_dir_files(seaon_dir, exts=settings.RMT_MEDIAEXT):
                    shutil.rmtree(seaon_dir)
                # 媒体文件夹
                media_dir = os.path.dirname(seaon_dir)
            else:
                media_dir = filedir
            # 检查并删除媒体文件夹，非根目录且目录大于二级，且没有媒体文件时才会删除
            if media_dir != '/' \
                    and media_dir.count('/') > 1 \
                    and not re.search(r'[a-zA-Z]:/$', media_dir) \
                    and not PathUtils.get_dir_files(media_dir, exts=settings.RMT_MEDIAEXT):
                shutil.rmtree(media_dir)
            return True, f"{file} 删除成功"
        except Exception as e:
            logger.error("删除源文件失败：%s" % str(e))
            return True, f"{file} 删除失败"

    def get_state(self):
        return self._enabled

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

    @eventmanager.register(EventType.MediaDeleted)
    def remote_sync_del(self, event: Event):
        """
        媒体库同步删除
        """
        if event:
            logger.info("收到命令，开始执行媒体库同步删除 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始媒体库同步删除 ...",
                              userid=event.event_data.get("user"))
        self.sync_del_by_log()

        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="媒体库同步删除完成！", userid=event.event_data.get("user"))

    @staticmethod
    def get_tmdbimage_url(path: str, prefix="w500"):
        if not path:
            return ""
        tmdb_image_url = f"https://{settings.TMDB_IMAGE_DOMAIN}"
        return tmdb_image_url + f"/t/p/{prefix}{path}"
