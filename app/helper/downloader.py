from typing import List

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.system import DownloaderConf
from app.schemas.types import SystemConfigKey


class DownloaderHelper:
    """
    下载器帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_downloaders(self) -> List[DownloaderConf]:
        """
        获取下载器
        """
        downloader_confs: List[dict] = self.systemconfig.get(SystemConfigKey.Downloaders)
        if not downloader_confs:
            return []
        return [DownloaderConf(**conf) for conf in downloader_confs]
