from typing import Optional

from app.helper.service import ServiceBaseHelper
from app.schemas import DownloaderConf, ServiceInfo
from app.schemas.types import SystemConfigKey, ModuleType


class DownloaderHelper(ServiceBaseHelper[DownloaderConf]):
    """
    下载器帮助类
    """

    def __init__(self):
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
        :param service_type: 下载器的类型名称（如 'qbittorrent', 'transmission'）
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        # 如果未提供 service 则通过 name 获取服务
        service = service or self.get_service(name=name)

        # 判断服务类型是否为指定类型
        return bool(service and service.type == service_type)
