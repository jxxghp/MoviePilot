from pathlib import Path
from typing import Dict

from cachetools import TTLCache, cached

from app.utils.singleton import Singleton


class PluginHelper(metaclass=Singleton):
    """
    插件市场管理，下载安装插件到本地
    """

    @cached(cache=TTLCache(maxsize=1, ttl=1800))
    def get_plugins(self) -> Dict[str, dict]:
        """
        获取Github所有最新插件列表
        """
        pass

    def download(self, name: str, dest: Path) -> bool:
        """
        下载插件到本地
        """
        pass

    def install(self, name: str) -> bool:
        """
        安装插件
        """
        pass
