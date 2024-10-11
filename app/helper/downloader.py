from typing import Optional, Union, Type

from app.helper.servicebase import ServiceBaseHelper
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.schemas import DownloaderConf, ServiceInfo
from app.schemas.types import SystemConfigKey


class DownloaderHelper(ServiceBaseHelper[DownloaderConf]):
    """
    下载器帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.Downloaders,
            conf_type=DownloaderConf,
            modules=["QbittorrentModule", "TransmissionModule"]
        )

    def _is_downloader(self, service_type: str, instance_type: Type, service: Optional[ServiceInfo] = None,
                       name: Optional[str] = None, instance: Optional[Union[Qbittorrent, Transmission]] = None) -> bool:
        """
        通用的下载器类型判断方法

        :param service_type: 下载器的类型名称（如 'qbittorrent', 'transmission'）
        :param instance_type: 实例类型 (如 Qbittorrent, Transmission)
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 下载器实例对象
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        # 如果传入了 instance，优先判断 instance 类型
        if isinstance(instance, instance_type):
            return True

        # 如果未提供 service 则通过 name 获取服务
        service = service or self.get_service(name=name)

        # 判断服务类型是否为指定类型
        return bool(service and service.type == service_type)

    def is_qbittorrent(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None,
                       instance: Optional[Union[Qbittorrent, Transmission]] = None) -> bool:
        """
        判断指定的下载器是否为 qbittorrent 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 下载器实例对象
        :return: 如果服务类型或实例为 qbittorrent，返回 True；否则返回 False
        """
        return self._is_downloader("qbittorrent", Qbittorrent, service, name, instance)

    def is_transmission(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None,
                        instance: Optional[Union[Qbittorrent, Transmission]] = None) -> bool:
        """
        判断指定的下载器是否为 transmission 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 下载器实例对象
        :return: 如果服务类型或实例为 transmission，返回 True；否则返回 False
        """
        return self._is_downloader("transmission", Transmission, service, name, instance)
