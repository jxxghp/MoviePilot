from typing import List, Tuple, Dict, Any

from app.core.event import eventmanager
from app.plugins import _PluginBase
from app.schemas.types import EventType


class TorrentRemover(_PluginBase):
    # 插件名称
    plugin_name = "自动删种"
    # 插件描述
    plugin_desc = "自动删除下载器中的下载任务。"
    # 插件图标
    plugin_icon = "torrent.png"
    # 主题色
    plugin_color = "#02853F"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrentremover_"
    # 加载顺序
    plugin_order = 8
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    downloader = None
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
        pass

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass

    @eventmanager.register(EventType.HistoryDeleted)
    def deletetorrent(self, event):
        """
        联动删除下载器中的下载任务
        """
        if not self._enabled:
            return
        event_info = event.event_data
        if not event_info:
            return

        # TODO 删除所有下载任务
