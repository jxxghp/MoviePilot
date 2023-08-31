import ipaddress
from typing import List, Tuple, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType, WebhookEventInfo
from app.schemas.types import EventType
from app.utils.ip import IpUtils


class SpeedLimiter(_PluginBase):
    # 插件名称
    plugin_name = "播放限速"
    # 插件描述
    plugin_desc = "外网播放媒体库视频时，自动对下载器进行限速。"
    # 插件图标
    plugin_icon = "SpeedLimiter.jpg"
    # 主题色
    plugin_color = "#183883"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "Shurelol"
    # 作者主页
    author_url = "https://github.com/Shurelol"
    # 插件配置项ID前缀
    plugin_config_prefix = "speedlimit_"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    _qb = None
    _tr = None
    _enabled: bool = False
    _notify: bool = False
    _interval: int = 60
    _downloader: list = []
    _play_up_speed: float = 0
    _play_down_speed: float = 0
    _noplay_up_speed: float = 0
    _noplay_down_speed: float = 0
    _bandwidth: float = 0
    _allocation_ratio: str = ""
    _auto_limit: bool = False
    _limit_enabled: bool = False
    # 不限速地址
    _unlimited_ips = {"ipv4": "0.0.0.0/0", "ipv6": "::/0"}
    # 当前限速状态
    _current_state = ""

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._play_up_speed = float(config.get("play_up_speed")) if config.get("play_up_speed") else 0
            self._play_down_speed = float(config.get("play_down_speed")) if config.get("play_down_speed") else 0
            self._noplay_up_speed = float(config.get("noplay_up_speed")) if config.get("noplay_up_speed") else 0
            self._noplay_down_speed = float(config.get("noplay_down_speed")) if config.get("noplay_down_speed") else 0
            try:
                # 总带宽
                self._bandwidth = int(float(config.get("bandwidth") or 0)) * 1000000
                # 自动限速开关
                self._auto_limit = True
            except Exception:
                self._bandwidth = 0

            # 限速服务开关
            self._limit_enabled = True if self._play_up_speed or self._play_down_speed or self._auto_limit else False
            self._allocation_ratio = config.get("allocation_ratio") or ""
            # 不限速地址
            self._unlimited_ips["ipv4"] = config.get("ipv4") or "0.0.0.0/0"
            self._unlimited_ips["ipv6"] = config.get("ipv6") or "::/0"
            if "0.0.0.0/0" in self._unlimited_ips["ipv4"] and "::/0" in self._unlimited_ips["ipv6"]:
                self._limit_enabled = False
            self._downloader = config.get("downloader") or []
            if self._downloader:
                if 'qbittorrent' in self._downloader:
                    self._qb = Qbittorrent()
                if 'transmission' in self._downloader:
                    self._tr = Transmission()

        # 移出现有任务
        self.stop_service()

        # 启动限速任务
        if self._enabled and self._limit_enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(func=self.check_playing_sessions,
                                    trigger='interval',
                                    seconds=self._interval,
                                    name="播放限速检查")
            self._scheduler.print_jobs()
            self._scheduler.start()
            logger.info("播放限速检查服务启动")

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
                                       'content': [
                                           {
                                               'component': 'VSelect',
                                               'props': {
                                                   'chips': True,
                                                   'multiple': True,
                                                   'model': 'downloader',
                                                   'label': '下载器',
                                                   'items': [
                                                       {'title': 'Qbittorrent', 'value': 'qbittorrent'},
                                                       {'title': 'Transmission', 'value': 'transmission'},
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
                                           'cols': 12,
                                           'md': 6
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'play_up_speed',
                                                   'label': '播放限速（上传）',
                                                   'placeholder': 'KB/s'
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
                                                   'model': 'play_down_speed',
                                                   'label': '播放限速（下载）',
                                                   'placeholder': 'KB/s'
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
                                                   'model': 'noplay_up_speed',
                                                   'label': '未播放限速（上传）',
                                                   'placeholder': 'KB/s'
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
                                                   'model': 'noplay_down_speed',
                                                   'label': '未播放限速（下载）',
                                                   'placeholder': 'KB/s'
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
                                                   'model': 'bandwidth',
                                                   'label': '智能限速上行带宽',
                                                   'placeholder': '设置上行带宽后，媒体服务器有媒体播放时根据上行带宽和媒体播放占用带宽计算上传限速数值。'
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
                                                   'model': 'allocation_ratio',
                                                   'label': '智能限速分配比例',
                                                   'placeholder': '多个下载器设置分配比例，如两个下载器设置1:2,留空均分'
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
                                                   'model': 'ipv4',
                                                   'label': '不限速地址范围（ipv4）',
                                                   'placeholder': '192.168.1.0/24'
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
                                                   'model': 'ipv6',
                                                   'label': '不限速地址范围（ipv6）',
                                                   'placeholder': 'FE80::/10'
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
                   "downloader": [],
                   "play_up_speed": 0,
                   "play_down_speed": 0,
                   "noplay_up_speed": 0,
                   "noplay_down_speed": 0,
                   "bandwidth": 0,
                   "allocation_ratio": "",
                   "ipv4": "",
                   "ipv6": "",
               }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.WebhookMessage)
    def check_playing_sessions(self, event: Event = None):
        """
        检查播放会话
        """
        if not self._qb and not self._tr:
            return
        if event:
            event_data: WebhookEventInfo = event.event_data
            if event_data.event not in ["playback.start", "PlaybackStart", "media.play"]:
                return
        # 当前播放的总比特率
        total_bit_rate = 0
        # 查询播放中会话
        playing_sessions = []
        if settings.MEDIASERVER == "emby":
            req_url = "{HOST}emby/Sessions?api_key={APIKEY}"
            try:
                res = Emby().get_data(req_url)
                if res and res.status_code == 200:
                    sessions = res.json()
                    for session in sessions:
                        if not self.__allow_access(self._unlimited_ips, session.get("RemoteEndPoint")) and \
                                session.get("NowPlayingItem") and not session.get("PlayState", {}).get("IsPaused"):
                            playing_sessions.append(session)
            except Exception as e:
                logger.error(f"获取Emby播放会话失败：{str(e)}")
            # 计算有效比特率
            for session in playing_sessions:
                if not IpUtils.is_private_ip(session.get("RemoteEndPoint")) \
                        and session.get("NowPlayingItem", {}).get("MediaType") == "Video":
                    total_bit_rate += int(session.get("NowPlayingItem", {}).get("Bitrate") or 0)
        elif settings.MEDIASERVER == "jellyfin":
            req_url = "{HOST}Sessions?api_key={APIKEY}"
            try:
                res = Jellyfin().get_data(req_url)
                if res and res.status_code == 200:
                    sessions = res.json()
                    for session in sessions:
                        if not self.__allow_access(self._unlimited_ips, session.get("RemoteEndPoint")) and \
                                session.get("NowPlayingItem") and not session.get("PlayState", {}).get("IsPaused"):
                            playing_sessions.append(session)
            except Exception as e:
                logger.error(f"获取Jellyfin播放会话失败：{str(e)}")
            # 计算有效比特率
            for session in playing_sessions:
                if not IpUtils.is_private_ip(session.get("RemoteEndPoint")) \
                        and session.get("NowPlayingItem", {}).get("MediaType") == "Video":
                    media_streams = session.get("NowPlayingItem", {}).get("MediaStreams") or []
                    for media_stream in media_streams:
                        total_bit_rate += int(media_stream.get("BitRate") or 0)
        elif settings.MEDIASERVER == "plex":
            _plex = Plex().get_plex()
            if _plex:
                sessions = _plex.sessions()
                for session in sessions:
                    bitrate = sum([m.bitrate or 0 for m in session.media])
                    playing_sessions.append({
                        "type": session.TAG,
                        "bitrate": bitrate,
                        "address": session.player.address
                    })
                # 计算有效比特率
                for session in playing_sessions:
                    if not self.__allow_access(self._unlimited_ips, session.get("address")) and \
                            IpUtils.is_private_ip(session.get("address")) \
                            and session.get("type") == "Video":
                        total_bit_rate += int(session.get("bitrate") or 0)

        if total_bit_rate:
            # 开启智能限速计算上传限速
            if self._auto_limit:
                play_up_speed = self.__calc_limit(total_bit_rate)
            else:
                play_up_speed = self._play_up_speed

            # 当前正在播放，开始限速
            self.__set_limiter(limit_type="播放", upload_limit=play_up_speed,
                               download_limit=self._play_down_speed)
        else:
            # 当前没有播放，取消限速
            self.__set_limiter(limit_type="未播放", upload_limit=self._noplay_up_speed,
                               download_limit=self._noplay_down_speed)

    def __calc_limit(self, total_bit_rate):
        """
        计算智能上传限速
        """
        residual_bandwidth = (self._bandwidth - total_bit_rate)
        if residual_bandwidth < 0:
            play_up_speed = 10
        else:
            play_up_speed = residual_bandwidth / 8 / 1024

        return play_up_speed

    def __set_limiter(self, limit_type: str, upload_limit: float, download_limit: float):
        """
        设置限速
        """
        if not self._qb and not self._tr:
            return
        state = f"U:{upload_limit},D:{download_limit}"
        if self._current_state == state:
            # 限速状态没有改变
            return
        else:
            self._current_state = state

        if upload_limit:
            text = f"上传：{upload_limit} KB/s"
        else:
            text = f"上传：未限速"
        if download_limit:
            text = f"{text}\n下载：{download_limit} KB/s"
        else:
            text = f"{text}\n下载：未限速"
        try:
            cnt = 0
            for download in self._downloader:
                if self._auto_limit and limit_type == "播放":
                    # 开启了播放智能限速
                    allocation_count = sum(self._allocation_ratio) if self._allocation_ratio else len(self._downloader)
                    if not self._allocation_ratio:
                        upload_limit = int(upload_limit / allocation_count)
                    else:
                        upload_limit = int(upload_limit * float(self._allocation_ratio[cnt]) / allocation_count)
                        cnt += 1

                if str(download) == 'qbittorrent':
                    if self._qb:
                        self._qb.set_speed_limit(download_limit=download_limit, upload_limit=upload_limit)
                        # 发送通知
                        if self._notify:
                            title = "【播放限速】"
                            if upload_limit or download_limit:
                                subtitle = f"Qbittorrent 开始{limit_type}限速"
                                self.post_message(
                                    mtype=NotificationType.MediaServer,
                                    title=title,
                                    text=f"{subtitle}\n{text}"
                                )
                            else:
                                self.post_message(
                                    mtype=NotificationType.MediaServer,
                                    title=title,
                                    text=f"Qbittorrent 已取消限速"
                                )
                    else:
                        if self._tr:
                            self._tr.set_speed_limit(download_limit=download_limit, upload_limit=upload_limit)
                            # 发送通知
                            if self._notify:
                                title = "【播放限速】"
                                if upload_limit or download_limit:
                                    subtitle = f"Transmission 开始{limit_type}限速"
                                    self.post_message(
                                        mtype=NotificationType.MediaServer,
                                        title=title,
                                        text=f"{subtitle}\n{text}"
                                    )
                                else:
                                    self.post_message(
                                        mtype=NotificationType.MediaServer,
                                        title=title,
                                        text=f"Transmission 已取消限速"
                                    )
        except Exception as e:
            logger.error(f"设置限速失败：{str(e)}")

    @staticmethod
    def __allow_access(allow_ips, ip):
        """
        判断IP是否合法
        :param allow_ips: 充许的IP范围 {"ipv4":, "ipv6":}
        :param ip: 需要检查的ip
        """
        if not allow_ips:
            return True
        try:
            ipaddr = ipaddress.ip_address(ip)
            if ipaddr.version == 4:
                if not allow_ips.get('ipv4'):
                    return True
                allow_ipv4s = allow_ips.get('ipv4').split(",")
                for allow_ipv4 in allow_ipv4s:
                    if ipaddr in ipaddress.ip_network(allow_ipv4):
                        return True
            elif ipaddr.ipv4_mapped:
                if not allow_ips.get('ipv4'):
                    return True
                allow_ipv4s = allow_ips.get('ipv4').split(",")
                for allow_ipv4 in allow_ipv4s:
                    if ipaddr.ipv4_mapped in ipaddress.ip_network(allow_ipv4):
                        return True
            else:
                if not allow_ips.get('ipv6'):
                    return True
                allow_ipv6s = allow_ips.get('ipv6').split(",")
                for allow_ipv6 in allow_ipv6s:
                    if ipaddr in ipaddress.ip_network(allow_ipv6):
                        return True
        except Exception:
            return False
        return False

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
            print(str(e))
