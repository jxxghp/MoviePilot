from typing import Optional

from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import NotificationConf
from app.schemas.system import ServiceInfo
from app.schemas.types import SystemConfigKey


class NotificationHelper(ServiceBaseHelper[NotificationConf]):
    """提供按持久化配置发现通知服务的能力。"""

    def __init__(self):
        """绑定通知配置键与配置模型。"""
        super().__init__(
            config_key=SystemConfigKey.Notifications,
            conf_type=NotificationConf,
        )

    def is_notification(
        self,
        service_type: Optional[str] = None,
        service: Optional[ServiceInfo] = None,
        name: Optional[str] = None,
    ) -> bool:
        """
        判断通知服务是否属于指定类型。

        :param service_type: 消息通知服务的类型名称
        :param service: 要判断的服务信息
        :param name: 未传入服务信息时用于查询的服务名称
        :return: 服务存在且类型匹配时返回 True
        """
        service = service or self.get_service(name=name)
        return bool(service and service.type == service_type)
