from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class DownloaderHelper:
    """
    下载器帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_downloaders(self) -> dict:
        """
        获取下载器
        """
        downloader_conf: dict = self.systemconfig.get(SystemConfigKey.Downloaders)
        if not downloader_conf:
            return {}
        return downloader_conf
