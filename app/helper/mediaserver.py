from typing import Optional

from app.helper.servicebase import ServiceBaseHelper
from app.schemas import MediaServerConf, ServiceInfo
from app.schemas.types import SystemConfigKey


class MediaServerHelper(ServiceBaseHelper[MediaServerConf]):
    """
    媒体服务器帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.MediaServers,
            conf_type=MediaServerConf,
            modules=["PlexModule", "EmbyModule", "JellyfinModule"]
        )

    def is_plex(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Plex 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 plex，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "plex" if service else False

    def is_emby(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Emby 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 emby，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "emby" if service else False

    def is_jellyfin(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Jellyfin 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 jellyfin，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "jellyfin" if service else False
