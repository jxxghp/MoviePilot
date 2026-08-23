from typing import Optional

from app.application.service import ServiceBaseHelper, get_service_configs
from app.schemas.system import NotificationConf, NotificationSwitchConf
from app.schemas.system import ServiceInfo
from app.schemas.types import MessageType, ModuleType, SystemConfigKey


class NotificationHelper(ServiceBaseHelper[NotificationConf]):
    """提供按持久化配置发现通知服务的能力。"""

    def __init__(self):
        """绑定通知配置和通知模块类型。"""
        super().__init__(
            config_key=SystemConfigKey.Notifications,
            conf_type=NotificationConf,
            module_type=ModuleType.Notification,
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


def get_notification_configs(
    include_disabled: bool = False,
) -> list[NotificationConf]:
    """返回通知配置列表，并按调用方需要决定是否包含禁用项。"""
    return list(
        NotificationHelper().get_configs(include_disabled=include_disabled).values()
    )


def get_notification_switch(mtype: MessageType) -> Optional[str]:
    """返回指定通知场景的目标范围。"""
    for switch in get_service_configs(
        SystemConfigKey.NotificationSwitchs,
        NotificationSwitchConf,
    ):
        if switch.type == mtype.value:
            return switch.action
    return None
