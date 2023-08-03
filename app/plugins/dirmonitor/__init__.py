from typing import List, Tuple, Dict, Any

from app.plugins import _PluginBase


class DirMonitor(_PluginBase):
    # 插件名称
    plugin_name = "目录监控"
    # 插件描述
    plugin_desc = "监控目录文件发生变化时实时整理到媒体库。"
    # 插件图标
    plugin_icon = "directory.png"
    # 主题色
    plugin_color = "#E0995E"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "dirmonitor_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    user_level = 1

    # 私有属性
    _monitor = None
    _enabled = False

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")

        # 停止现有任务
        self.stop_service()

        # TODO 启动任务

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
        """
        退出插件
        """
        pass
