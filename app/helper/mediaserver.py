from typing import Optional, Union, Type

from app.helper.servicebase import ServiceBaseHelper
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
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

    def _is_media_server(self, service_type: str, instance_type: Type, service: Optional[ServiceInfo] = None,
                         name: Optional[str] = None, instance: Optional[Union[Plex, Emby, Jellyfin]] = None) -> bool:
        """
        通用的媒体服务器类型判断方法

        :param service_type: 媒体服务器的类型名称（如 'plex', 'emby', 'jellyfin'）
        :param instance_type: 实例类型 (如 Plex, Emby, Jellyfin)
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 媒体服务器实例对象
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        # 如果传入了 instance，优先判断 instance 类型
        if isinstance(instance, instance_type):
            return True

        # 如果未提供 service 则通过 name 获取服务
        service = service or self.get_service(name=name)

        # 判断服务类型是否为指定类型
        return bool(service and service.type == service_type)

    def is_plex(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None,
                instance: Optional[Union[Plex, Emby, Jellyfin]] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Plex 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 媒体服务器实例对象
        :return: 如果服务类型或实例为 Plex，返回 True；否则返回 False
        """
        return self._is_media_server("plex", Plex, service, name, instance)

    def is_emby(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None,
                instance: Optional[Union[Plex, Emby, Jellyfin]] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Emby 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 媒体服务器实例对象
        :return: 如果服务类型或实例为 Emby，返回 True；否则返回 False
        """
        return self._is_media_server("emby", Emby, service, name, instance)

    def is_jellyfin(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None,
                    instance: Optional[Union[Plex, Emby, Jellyfin]] = None) -> bool:
        """
        判断指定的媒体服务器是否为 Jellyfin 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 媒体服务器实例对象
        :return: 如果服务类型或实例为 Jellyfin，返回 True；否则返回 False
        """
        return self._is_media_server("jellyfin", Jellyfin, service, name, instance)
