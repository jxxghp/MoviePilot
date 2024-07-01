from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class MediaServerHelper:
    """
    媒体服务器帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_mediaservers(self) -> dict:
        """
        获取媒体服务器
        """
        mediaserver_conf: dict = self.systemconfig.get(SystemConfigKey.MediaServers)
        if not mediaserver_conf:
            return {}
        return mediaserver_conf
