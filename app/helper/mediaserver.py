from typing import List

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import MediaServerConf
from app.schemas.types import SystemConfigKey


class MediaServerHelper:
    """
    媒体服务器帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_mediaservers(self) -> List[MediaServerConf]:
        """
        获取媒体服务器
        """
        mediaserver_confs: List[dict] = self.systemconfig.get(SystemConfigKey.MediaServers)
        if not mediaserver_confs:
            return []
        return [MediaServerConf(**conf) for conf in mediaserver_confs]