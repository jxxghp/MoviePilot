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
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.themoviedb.tmdbv3api import Episode
from app.plugins import _PluginBase
from app.schemas.types import NotificationType, EventType
from app.utils.path_utils import PathUtils


class MediaSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "媒体库同步删除"
    # 插件描述
    plugin_desc = "媒体库删除媒体后同步删除历史记录或源文件。"
    # 插件图标
    plugin_icon = "mediasyncdel.png"
    # 主题色
    plugin_color = "#ff1a1a"
    # 插件版本
    plugin_version = "1.0"
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

    def init_plugin(self, config: dict = None):
        self._transferhis = TransferHistoryOper()
        self.episode = Episode()

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
        return [{
            "cmd": "/sync_del",
            "event": EventType.HistoryDeleted,
            "desc": "媒体库同步删除",
            "data": {}
        }]

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
                                            'text': '同步方式分为日志同步和Scripter X。日志同步需要配置执行周期，默认30分钟执行一次。'
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
            "sync_type": "log",
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
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{htype}'
                                        },
                                        {
                                            'component': 'VCardSubtitle',
                                            'props': {
                                                'class': 'pa-2 font-bold break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'class': 'pa-0 px-2'
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
    def sync_del_by_plugin(self, event):
        """
        emby删除媒体库同步删除历史记录
        Scripter X插件
        """
        if not self._enabled:
            return
        event_data = event.event_data
        event_type = event_data.event
        if not event_type or str(event_type) != 'media_del':
            return

        # 是否虚拟标识
        item_isvirtual = event_data.item_isvirtual
        if not item_isvirtual:
            logger.error("item_isvirtual参数未配置，为防止误删除，暂停插件运行")
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

        # 读取历史记录
        history = self.get_data('history') or []

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
        if season_num and str(season_num).isdigit() and int(season_num) < 10:
            season_num = f'0{season_num}'
        # 集数
        episode_num = event_data.episode_id
        if episode_num and str(episode_num).isdigit() and int(episode_num) < 10:
            episode_num = f'0{episode_num}'

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

        # 删除电影
        if media_type == "Movie":
            msg = f'电影 {media_name} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id)
        # 删除电视剧
        elif media_type == "Series":
            msg = f'剧集 {media_name} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id)
        # 删除季 S02
        elif media_type == "Season":
            if not season_num or not str(season_num).isdigit():
                logger.error(f"{media_name} 季同步删除失败，未获取到具体季")
                return
            msg = f'剧集 {media_name} S{season_num} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id)
        # 删除剧集S02E02
        elif media_type == "Episode":
            if not season_num or not str(season_num).isdigit() or not episode_num or not str(episode_num).isdigit():
                logger.error(f"{media_name} 集同步删除失败，未获取到具体集")
                return
            msg = f'剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}'
            transfer_history: List[TransferHistory] = self._transferhis.get_by(tmdbid=tmdb_id)
        else:
            return

        logger.info(f"正在同步删除{msg}")

        if not transfer_history:
            logger.warn(f"{media_type} {media_name} 未获取到可删除数据")
            return

        # 开始删除
        image = 'https://emby.media/notificationicon.png'
        year = None
        for transferhis in transfer_history:
            image = transferhis.image
            year = transferhis.year
            # 删除种子任务
            if self._del_source:
                del_source = False
                if transferhis.download_hash:
                    try:
                        # 判断种子是否被删除完
                        self.handle_torrent(history_id=transferhis.id,
                                            src=transferhis.src,
                                            torrent_hash=transferhis.download_hash)
                    except Exception as e:
                        logger.error("删除种子失败，尝试删除源文件：%s" % str(e))
                        del_source = True

                # 直接删除源文件
                if del_source:
                    source_name = os.path.basename(transferhis.src)
                    source_path = str(transferhis.src).replace(source_name, "")
                    self.delete_media_file(filedir=source_path,
                                           filename=source_name)

        logger.info(f"同步删除 {msg} 完成！")

        # 发送消息
        if self._notify:
            if media_type == "Episode":
                # 根据tmdbid获取图片
                image = self.episode.images(tv_id=tmdb_id,
                                            season_num=season_num,
                                            episode_num=episode_num)
            # 发送通知
            self.post_message(
                mtype=NotificationType.MediaServer,
                title="媒体库同步删除任务完成",
                image=image,
                text=f"{msg}\n"
                     f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"
            )

        history.append({
            "type": "电影" if media_type == "Movie" else "电视剧",
            "title": media_name,
            "year": year,
            "path": media_path,
            "season": season_num,
            "episode": episode_num,
            "image": image,
            "del_time": str(datetime.datetime.now())
        })

        # 保存历史
        self.save_data("history", history)

        self.save_data("last_time", datetime.datetime.now())

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
            del_time = del_media.get("time")
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
                    mtype="电影",
                    title=media_name,
                    year=media_year)
            # 删除电视剧
            elif media_type == "Series":
                msg = f'剧集 {media_name}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    mtype="电视剧",
                    title=media_name,
                    year=media_year)
            # 删除季 S02
            elif media_type == "Season":
                msg = f'剧集 {media_name} {media_season}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    mtype="电视剧",
                    title=media_name,
                    year=media_year)
            # 删除剧集S02E02
            elif media_type == "Episode":
                msg = f'剧集 {media_name} {media_season}{media_episode}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    mtype="电视剧",
                    title=media_name,
                    year=media_year)
            else:
                continue

            logger.info(f"正在同步删除 {msg}")

            if not transfer_history:
                logger.info(f"未获取到 {msg} 转移记录")
                continue

            logger.info(f"获取到删除历史记录数量 {len(transfer_history)}")

            # 开始删除
            image = 'https://emby.media/notificationicon.png'
            for transferhis in transfer_history:
                image = transferhis.image
                self._transferhis.delete(transferhis.id)
                # 删除种子任务
                if self._del_source:
                    del_source = False
                    if transferhis.download_hash:
                        try:
                            # 判断种子是否被删除完
                            self.handle_torrent(history_id=transferhis.id,
                                                src=transferhis.src,
                                                torrent_hash=transferhis.download_hash)
                        except Exception as e:
                            logger.error("删除种子失败，尝试删除源文件：%s" % str(e))
                            del_source = True

                    # 直接删除源文件
                    if del_source:
                        source_name = os.path.basename(transferhis.src)
                        source_path = str(transferhis.src).replace(source_name, "")
                        self.delete_media_file(filedir=source_path,
                                               filename=source_name)

            logger.info(f"同步删除 {msg} 完成！")

            # 发送消息
            if self._notify:
                self.post_message(
                    mtype=NotificationType.MediaServer,
                    title="媒体库同步删除任务完成",
                    text=f"{msg}\n"
                         f"数量 {len(transfer_history)}\n"
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
                "del_time": del_time
            })

        # 保存历史
        self.save_data("history", history)

        self.save_data("last_time", datetime.datetime.now())

    def handle_torrent(self, history_id: int, src: str, torrent_hash: str):
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

        # 删除历史标志
        del_history = False
        # 删除种子标志
        delete_flag = True

        # 是否需要暂停源下载器种子
        stop_from = False

        # 如果有转种记录，则删除转种后的下载任务
        if transfer_history and isinstance(transfer_history, dict):
            download = transfer_history['to_download']
            download_id = transfer_history['to_download_id']
            delete_source = transfer_history['delete_source']
            del_history = True

            # 转种后未删除源种时，同步删除源种
            if not delete_source:
                logger.info(f"{history_key} 转种时未删除源下载任务，开始删除源下载任务…")

                try:
                    dl_files = self.chain.torrent_files(tid=torrent_hash)
                    if not dl_files:
                        logger.info(f"未获取到 {settings.DOWNLOADER} - {torrent_hash} 种子文件，种子已被删除")
                    else:
                        for dl_file in dl_files:
                            dl_file_name = dl_file.get("name")
                            torrent_file = os.path.join(src, os.path.basename(dl_file_name))
                            if Path(torrent_file).exists():
                                logger.warn(f"种子有文件被删除，种子文件{torrent_file}暂未删除，暂停种子")
                                delete_flag = False
                                stop_from = True
                                break
                    if delete_flag:
                        logger.info(f"删除下载任务：{settings.DOWNLOADER} - {torrent_hash}")
                        self.chain.remove_torrents(torrent_hash)
                except Exception as e:
                    logger.error(f"删除源下载任务 {history_key} 失败: {str(e)}")

        # 如果是False则说明种子文件没有完全被删除，暂停种子，暂不处理
        if delete_flag:
            try:
                dl_files = self.chain.torrent_files(tid=download_id)
                if not dl_files:
                    logger.info(f"未获取到 {download} - {download_id} 种子文件，种子已被删除")
                else:
                    for dl_file in dl_files:
                        dl_file_name = dl_file.get("name")
                        if not stop_from:
                            torrent_file = os.path.join(src, os.path.basename(dl_file_name))
                            if Path(torrent_file).exists():
                                logger.info(f"种子有文件被删除，种子文件{torrent_file}暂未删除，暂停种子")
                                delete_flag = False
                                break
                if delete_flag:
                    # 删除源下载任务或转种后下载任务
                    logger.info(f"删除下载任务：{download} - {download_id}")
                    self.chain.remove_torrents(download_id)

                    # 删除转移记录
                    self._transferhis.delete(history_id)

                    # 删除转种记录
                    if del_history:
                        self.del_data(key=history_key, plugin_id=plugin_id)

                    # 处理辅种
                    self.__del_seed(download=download, download_id=download_id, action_flag="del")
            except Exception as e:
                logger.error(f"删除转种辅种下载任务失败: {str(e)}")

        # 判断是否暂停
        if not delete_flag:
            logger.error("开始暂停种子")
            # 暂停种子
            if stop_from:
                # 暂停源种
                self.chain.stop_torrents(torrent_hash)
                logger.info(f"种子：{settings.DOWNLOADER} - {torrent_hash} 暂停")

            # 转种
            self.chain.stop_torrents(download_id)
            logger.info(f"转种：{download} - {download_id} 暂停")

            # 辅种
            self.__del_seed(download=download, download_id=download_id, action_flag="stop")

    def __del_seed(self, download, download_id, action_flag):
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
                if int(downloader) == download:
                    for torrent in torrents:
                        # 删除辅种
                        if action_flag == "del":
                            logger.info(f"删除辅种：{downloader} - {torrent}")
                            self.chain.remove_torrents(torrent)
                        # 暂停辅种
                        if action_flag == "stop":
                            self.chain.stop_torrents(torrent)
                            logger.info(f"辅种：{downloader} - {torrent} 暂停")

                    # 删除本下载器辅种历史
                    if action_flag == "del":
                        del history
                    break

            # 更新辅种历史
            self.save_data(key=history_key,
                           value=seed_history,
                           plugin_id=plugin_id)

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
    def parse_jellyfin_log(last_time):
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
    def delete_media_file(filedir, filename):
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
