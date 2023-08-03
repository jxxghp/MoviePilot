import datetime
import json
import os
import re
import time
from typing import List, Tuple, Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType, EventType
from app.utils.http import RequestUtils


class MediaSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "媒体库同步删除"
    # 插件描述
    plugin_desc = "媒体库删除媒体后同步删除历史记录或源文件。"
    # 插件图标
    plugin_icon = "sync.png"
    # 主题色
    plugin_color = "#53BA47"
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
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled = False
    _cron: str = ""
    _notify = False
    _del_source = False
    _exclude_path = None

    _transferhis = None

    def init_plugin(self, config: dict = None):
        self._transferhis = TransferHistoryOper()

        # 停止现有任务
        self.stop_service()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._del_source = config.get("del_source")
            self._exclude_path = config.get("exclude_path")

        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.sync_del,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="媒体库同步删除")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")
            else:
                self._scheduler.add_job(self.sync_del, "interval", minutes=30, name="媒体库同步删除")

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
                                    'md': 6
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
                                            'model': 'exclude_path',
                                            'label': '排除路径'
                                        }
                                    }
                                ]
                            }
                        ]
                    },

                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "del_source": False,
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

    def sync_del(self):
        """
        emby删除媒体库同步删除历史记录
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
                    year=media_year,
                    season=media_season)
            # 删除剧集S02E02
            elif media_type == "Episode":
                msg = f'剧集 {media_name} {media_season}{media_episode}'
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    mtype="电视剧",
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
            for transferhis in transfer_history:
                image = transferhis.image
                self._transferhis.delete(transferhis.id)
                # 删除种子任务
                if self._del_source and transferhis.download_hash:
                    self.chain.remove_torrents(transferhis.download_hash)

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

    @staticmethod
    def parse_emby_log(last_time):
        # emby host
        emby_host = settings.EMBY_HOST
        if emby_host:
            if not emby_host.endswith("/"):
                emby_host += "/"
            if not emby_host.startswith("http"):
                emby_host = "http://" + emby_host

        # emby 日志url
        log_url = "%sSystem/Logs/embyserver.txt?api_key=%s" % (emby_host, settings.EMBY_API_KEY)
        log_res = RequestUtils().get_res(url=log_url)

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
            mid = match[4]

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
                "id": mid,
                "season": season,
                "episode": episode,
            }
            logger.debug(f"解析到删除媒体：{json.dumps(media)}")
            del_medias.append(media)

        return del_medias

    @staticmethod
    def parse_jellyfin_log(last_time):
        # jellyfin host
        jellyfin_host = settings.JELLYFIN_HOST
        if jellyfin_host:
            if not jellyfin_host.endswith("/"):
                jellyfin_host += "/"
            if not jellyfin_host.startswith("http"):
                jellyfin_host = "http://" + jellyfin_host

        # jellyfin 日志url
        log_url = "%sSystem/Logs/jellyfinserver.txt?api_key=%s" % (jellyfin_host, settings.JELLYFIN_API_KEY)
        log_res = RequestUtils().get_res(url=log_url)

        if not log_res or log_res.status_code != 200:
            logger.error("获取jellyfin日志失败，请检查服务器配置")
            return []

        # 正则解析删除的媒体信息
        pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{3}) Info App: Removing item from database, Type: (\w+), Name: (.*), Path: (.*), Id: (\d+)'
        matches = re.findall(pattern, log_res.text)

        del_medias = []
        # 循环获取媒体信息
        for match in matches:
            mtime = match[0]
            # 排除已处理的媒体信息
            if time < last_time:
                continue

            mtype = match[1]
            name = match[2]
            path = match[3]
            mid = match[4]

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
                "id": mid,
                "season": season,
                "episode": episode,
            }
            logger.debug(f"解析到删除媒体：{json.dumps(media)}")
            del_medias.append(media)

        return del_medias

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
        self.sync_del()

        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="媒体库同步删除完成！", userid=event.event_data.get("user"))
