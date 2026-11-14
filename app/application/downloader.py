"""
已配置下载服务的发现、实例管理与能力选择。
"""

from typing import Optional

from app.application.service import ServiceBaseHelper
from app.schemas.system import DownloaderConf, ServiceInfo
from app.schemas.types import ModuleType, SystemConfigKey


class DownloaderHelper(ServiceBaseHelper[DownloaderConf]):
    """
    下载器帮助类
    """

    def __init__(self) -> None:
        """
        绑定下载器配置和下载器模块类型。
        """
        super().__init__(
            config_key=SystemConfigKey.Downloaders,
            conf_type=DownloaderConf,
            module_type=ModuleType.Downloader
        )

    def is_downloader(
            self,
            service_type: Optional[str] = None,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
    ) -> bool:
        """
        通用的下载器类型判断方法
        :param service_type: 下载器的类型名称（如 'qbittorrent', 'transmission', 'rtorrent'）
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        service = service or self.get_service(name=name)

        return bool(service and service.type == service_type)
