from apscheduler.schedulers.background import BackgroundScheduler

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.config import settings
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional, Union
from app.log import logger
from app.schemas import NotificationType, TransferTorrent, DownloadingTorrent
from app.schemas.types import TorrentStatus, MessageChannel
from app.utils.string import StringUtils


class DownloadingMsg(_PluginBase):
    # 插件名称
    plugin_name = "下载进度推送"
    # 插件描述
    plugin_desc = "定时推送正在下载进度。"
    # 插件图标
    plugin_icon = "downloadmsg.png"
    # 主题色
    plugin_color = "#3DE75D"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloading_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _seconds = None
    _type = None
    _adminuser = None
    _downloadhis = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._seconds = config.get("seconds") or 300
            self._type = config.get("type") or 'admin'
            self._adminuser = config.get("adminuser")

            # 加载模块
        if self._enabled:
            self._downloadhis = DownloadHistoryOper()
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._seconds:
                try:
                    self._scheduler.add_job(func=self.__downloading,
                                            trigger='interval',
                                            seconds=int(self._seconds),
                                            name="下载进度推送")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __downloading(self):
        """
        定时推送正在下载进度
        """
        # 正在下载种子
        torrents = DownloadChain().list_torrents(status=TorrentStatus.DOWNLOADING)
        if not torrents:
            logger.info("当前没有正在下载的任务！")
            return
            # 推送用户
        if self._type == "admin" or self._type == "both":
            if not self._adminuser:
                logger.error("未配置管理员用户")
                return

            for username in str(self._adminuser).split(","):
                self.__send_msg(torrents=torrents, username=username)

        if self._type == "user" or self._type == "both":
            user_torrents = {}
            # 根据正在下载种子hash获取下载历史
            for torrent in torrents:
                downloadhis = self._downloadhis.get_by_hash(download_hash=torrent.hash)
                if not downloadhis:
                    logger.warn(f"种子 {torrent.hash} 未获取到MoviePilot下载历史，无法推送下载进度")
                    continue
                if not downloadhis.username:
                    logger.debug(f"种子 {torrent.hash} 未获取到下载用户记录，无法推送下载进度")
                    continue
                user_torrent = user_torrents.get(downloadhis.username) or []
                user_torrent.append(torrent)
                user_torrents[downloadhis.username] = user_torrent

            if not user_torrents or not user_torrents.keys():
                logger.warn("未获取到用户下载记录，无法推送下载进度")
                return

            # 推送用户下载任务进度
            for username in list(user_torrents.keys()):
                if not username:
                    continue
                # 如果用户是管理员，无需重复推送
                if (self._type == "admin" or self._type == "both") and self._adminuser and username in str(
                        self._adminuser).split(","):
                    logger.debug("管理员已推送")
                    continue

                user_torrent = user_torrents.get(username)
                if not user_torrent:
                    logger.warn(f"未获取到用户 {username} 下载任务")
                    continue
                self.__send_msg(torrents=user_torrent,
                                username=username)

        if self._type == "all":
            self.__send_msg(torrents=torrents)

    def __send_msg(self, torrents: Optional[List[Union[TransferTorrent, DownloadingTorrent]]], username: str = None):
        """
        发送消息
        """
        title = f"共 {len(torrents)} 个任务正在下载："
        messages = []
        index = 1
        channel_value = None
        for torrent in torrents:
            year = None
            name = None
            se = None
            ep = None
            # 先查询下载记录，没有再识别
            downloadhis = self._downloadhis.get_by_hash(download_hash=torrent.hash)
            if downloadhis:
                name = downloadhis.title
                year = downloadhis.year
                se = downloadhis.seasons
                ep = downloadhis.episodes
                if not channel_value:
                    channel_value = downloadhis.channel
            else:
                try:
                    context = MediaChain().recognize_by_title(title=torrent.title)
                    if not context or not context.media_info:
                        continue
                    media_info = context.media_info
                    year = media_info.year
                    name = media_info.title
                    if media_info.number_of_seasons:
                        se = f"S{str(media_info.number_of_seasons).rjust(2, '0')}"
                    if media_info.number_of_episodes:
                        ep = f"E{str(media_info.number_of_episodes).rjust(2, '0')}"
                except Exception as e:
                    print(str(e))

            # 拼装标题
            if year:
                media_name = "%s (%s) %s%s" % (name, year, se, ep)
            elif name:
                media_name = "%s %s%s" % (name, se, ep)
            else:
                media_name = torrent.title

            if not self._adminuser or username not in str(self._adminuser).split(","):
                # 下载用户发送精简消息
                messages.append(f"{index}. {media_name} {round(torrent.progress, 1)}%")
            else:
                messages.append(f"{index}. {media_name}\n"
                                f"{torrent.title} "
                                f"{StringUtils.str_filesize(torrent.size)} "
                                f"{round(torrent.progress, 1)}%")
            index += 1

        # 用户消息渠道
        if channel_value:
            channel = next(
                (channel for channel in MessageChannel.__members__.values() if channel.value == channel_value), None)
        else:
            channel = None
        self.post_message(mtype=NotificationType.Download,
                          channel=channel,
                          title=title,
                          text="\n".join(messages),
                          userid=username)

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
                                           'cols': 12
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
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'seconds',
                                                   'label': '执行间隔',
                                                   'placeholder': '单位（秒）'
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
                                                   'model': 'adminuser',
                                                   'label': '管理员用户',
                                                   'placeholder': '多个用户,分割'
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
                                               'component': 'VSelect',
                                               'props': {
                                                   'model': 'type',
                                                   'label': '推送类型',
                                                   'items': [
                                                       {'title': '管理员', 'value': 'admin'},
                                                       {'title': '下载用户', 'value': 'user'},
                                                       {'title': '管理员和下载用户', 'value': 'both'},
                                                       {'title': '所有用户', 'value': 'all'}
                                                   ]
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
                   "seconds": 300,
                   "adminuser": "",
                   "type": "admin"
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
