from typing import Any, List, Dict, Tuple

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.plugins import _PluginBase
from app.schemas import TransferInfo, RefreshMediaItem
from app.schemas.types import EventType


class MediaServerRefresh(_PluginBase):
    # 插件名称
    plugin_name = "媒体库服务器刷新"
    # 插件描述
    plugin_desc = "入库后自动刷新Emby/Jellyfin/Plex服务器海报墙。"
    # 插件图标
    plugin_icon = "refresh2.png"
    # 主题色
    plugin_color = "#347180"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediaserverrefresh_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")

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
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def refresh(self, event: Event):
        """
        发送通知消息
        """
        if not self._enabled:
            return

        event_info: dict = event.event_data
        if not event_info:
            return

        # 刷新媒体库
        if not settings.MEDIASERVER:
            return

        # 入库数据
        transferinfo: TransferInfo = event_info.get("transferinfo")
        mediainfo: MediaInfo = event_info.get("mediainfo")
        items = [
            RefreshMediaItem(
                title=mediainfo.title,
                year=mediainfo.year,
                type=mediainfo.type,
                category=mediainfo.category,
                target_path=transferinfo.target_path
            )
        ]
        # Emby
        if "emby" in settings.MEDIASERVER:
            Emby().refresh_library_by_items(items)

        # Jeyllyfin
        if "jellyfin" in settings.MEDIASERVER:
            # FIXME Jellyfin未找到刷新单个项目的API
            Jellyfin().refresh_root_library()

        # Plex
        if "plex" in settings.MEDIASERVER:
            Plex().refresh_library_by_items(items)

    def stop_service(self):
        """
        退出插件
        """
        pass
