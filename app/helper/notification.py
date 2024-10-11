from typing import Optional, Union, TYPE_CHECKING

from app.helper.servicebase import ServiceBaseHelper
from app.schemas import NotificationConf, ServiceInfo
from app.schemas.types import SystemConfigKey

if TYPE_CHECKING:
    from app.modules.slack import Slack
    from app.modules.synologychat import SynologyChat
    from app.modules.telegram import Telegram
    from app.modules.vocechat import VoceChat
    from app.modules.wechat import WeChat


class NotificationHelper(ServiceBaseHelper[NotificationConf]):
    """
    消息通知帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.Notifications,
            conf_type=NotificationConf,
            modules=[
                "WechatModule",
                "WebPushModule",
                "VoceChatModule",
                "TelegramModule",
                "SynologyChatModule",
                "SlackModule"
            ]
        )

    def _is_notification_service(
            self,
            service_type: Optional[str] = None,
            instance_type: Optional[str] = None,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional[Union['WeChat', 'VoceChat', 'Telegram', 'SynologyChat', 'Slack']] = None
    ) -> bool:
        """
        通用的消息通知服务类型判断方法

        :param service_type: 消息通知服务的类型名称（如 'wechat', 'voicechat', 'telegram', 等）
        :param instance_type: 实例类型名称 (如 'WeChat', 'VoceChat', 'Telegram', 等)
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        # 如果传入了 instance，优先判断 instance 类型
        if instance and instance.__class__.__name__ == instance_type:
            return True

        # 如果未提供 service 则通过 name 获取服务
        service = service or self.get_service(name=name)

        # 判断服务类型是否为指定类型
        return bool(service and service.type == service_type)

    def is_wechat(
            self,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional['WeChat'] = None
    ) -> bool:
        """
        判断指定的消息通知服务是否为 WeChat 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为 WeChat，返回 True；否则返回 False
        """
        return self._is_notification_service("wechat", "WeChat", service, name, instance)

    def is_webpush(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 WebPush 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 WebPush，返回 True；否则返回 False
        """
        # WebPush 不支持实例类型判断，因此只通过服务类型判断
        return self._is_notification_service("webpush", None, service, name)

    def is_voicechat(
            self,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional['VoceChat'] = None
    ) -> bool:
        """
        判断指定的消息通知服务是否为 VoceChat 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为 VoceChat，返回 True；否则返回 False
        """
        return self._is_notification_service("voicechat", "VoceChat", service, name, instance)

    def is_telegram(
            self,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional['Telegram'] = None
    ) -> bool:
        """
        判断指定的消息通知服务是否为 Telegram 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为 Telegram，返回 True；否则返回 False
        """
        return self._is_notification_service("telegram", "Telegram", service, name, instance)

    def is_synologychat(
            self,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional['SynologyChat'] = None
    ) -> bool:
        """
        判断指定的消息通知服务是否为 SynologyChat 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为 SynologyChat，返回 True；否则返回 False
        """
        return self._is_notification_service("synologychat", "SynologyChat", service, name, instance)

    def is_slack(
            self,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
            instance: Optional['Slack'] = None
    ) -> bool:
        """
        判断指定的消息通知服务是否为 Slack 类型

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :param instance: 通知服务实例对象
        :return: 如果服务类型或实例为 Slack，返回 True；否则返回 False
        """
        return self._is_notification_service("slack", "Slack", service, name, instance)
