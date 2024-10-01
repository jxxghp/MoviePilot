from typing import Optional

from app.helper.servicebase import ServiceBaseHelper
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

    def is_qbittorrent(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的下载器是否为 qbittorrent 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 qbittorrent，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "qbittorrent" if service else False

    def is_transmission(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的下载器是否为 transmission 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 transmission，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "transmission" if service else False
