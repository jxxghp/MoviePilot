"""
通知服务的配置发现、实例管理与渠道选择。
"""

from collections.abc import Callable
from typing import Any, Literal, Optional

from app.application.messaging.channel.catalog import interaction_channel
from app.application.service import ServiceBaseHelper, get_service_configs
from app.schemas.system import NotificationConf, NotificationSwitchConf, ServiceInfo
from app.schemas.types import MessageType, ModuleType, NotificationChannel, SystemConfigKey

type InactiveChannelManager = Callable[[str, str, dict[str, Any]], dict[str, Any] | None]

_inactive_channel_manager: InactiveChannelManager | None = None


def configure_inactive_channel_manager(manager: InactiveChannelManager | None) -> None:
    """
    由宿主装配无需启动后台连接的渠道管理入口。
    """
    global _inactive_channel_manager
    _inactive_channel_manager = manager


def manage_inactive_channel(channel: str, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """
    仅在没有运行模块处理时执行宿主支持的管理动作。
    """
    return _inactive_channel_manager(channel, action, params) if _inactive_channel_manager else None


class NotificationHelper(ServiceBaseHelper[NotificationConf]):
    """
    提供按持久化配置发现通知服务的能力。
    """

    def __init__(self) -> None:
        """
        绑定通知配置和通知模块类型。
        """
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
    """
    返回通知配置列表，并按调用方需要决定是否包含禁用项。
    """
    return list(
        NotificationHelper().get_configs(include_disabled=include_disabled).values()
    )


def channel_interaction_is_enabled(channel: NotificationChannel, source: str) -> bool:
    """
    按有效连接配置检查机器人入站，兼容没有独立开关的旧入口。
    """
    if channel in {NotificationChannel.Web, NotificationChannel.WebAgent}:
        return True
    config = NotificationHelper().get_configs(include_disabled=True).get(source)
    if config is None:
        return True
    return bool(config.enabled) and config.interaction_enabled is not False and interaction_channel(config.type) == channel.value


def get_notification_switch(mtype: MessageType) -> str:
    """
    返回指定通知场景的目标范围，缺少配置时默认仅管理员。
    """
    for switch in get_service_configs(
        SystemConfigKey.NotificationSwitchs,
        NotificationSwitchConf,
    ):
        if switch.type == mtype.value:
            return switch.action or "admin"
    return "admin"


def get_notification_missing_user_policy(mtype: MessageType | None) -> Literal["admin", "skip"]:
    """
    按消息类型读取无操作用户时的策略，旧配置继续回退管理员。

    :param mtype: 已确定的通知事件类型，为空时保留默认行为
    :return: admin 回退管理员，skip 跳过个人通知分支
    """
    if mtype is not None:
        for switch in get_service_configs(SystemConfigKey.NotificationSwitchs, NotificationSwitchConf):
            if switch.type == mtype.value:
                return switch.missing_user
    return "admin"
