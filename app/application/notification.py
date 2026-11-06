"""
通知服务的配置发现、实例管理与渠道选择。
"""

from typing import Optional

from app.application.configuration import get_configured_system_config
from app.application.service import ServiceBaseHelper, get_service_configs
from app.schemas.message import Message, MessageNotificationContext
from app.schemas.system import NotificationConf, NotificationPoliciesConf, NotificationSwitchConf, ServiceInfo
from app.schemas.types import MessageType, ModuleType, SystemConfigKey


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


def apply_notification_policy(message: Message) -> Optional[Message]:
    """
    在宿主渠道及插件分流前应用主动通知策略，不过滤明确的手动回复。

    :param message: 已完成历史保存且尚未分配接收人的消息
    :return: 允许投递的原消息或简短副本；被策略关闭时为 None
    """
    context = message.notification_context
    if context is None or context.trigger != "automatic":
        return message
    value = get_configured_system_config().get(SystemConfigKey.NotificationPolicies)
    policy = NotificationPoliciesConf.model_validate(value or {}).agent_transfer_takeover
    if policy == "off":
        return None
    if policy == "attention":
        if context.stage not in {"attention", "unknown"}:
            return None
        return message.model_copy(update={"text": context.summary or "任务状态待核实，请查看整理历史。"})
    return message


def agent_transfer_notification_key(context: MessageNotificationContext) -> str:
    """
    由原始任务、结算版本和通知阶段构造可跨重启复用的幂等键。

    :param context: 宿主生成并校验过的自动整理接管身份
    :return: 不包含标题、文件路径或通知目标的稳定事件键
    """
    return f"agent.transfer.notification:{context.task_id}:{context.settlement_revision}:{context.stage}:v1"
