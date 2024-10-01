from typing import Optional

from app.helper.servicebase import ServiceBaseHelper
from app.schemas import NotificationConf, ServiceInfo
from app.schemas.types import SystemConfigKey


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

    def is_wechat(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 Wechat 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 wechat，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "wechat" if service else False

    def is_webpush(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 WebPush 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 webpush，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "webpush" if service else False

    def is_voicechat(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 VoiceChat 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 voicechat，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "voicechat" if service else False

    def is_telegram(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 Telegram 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 telegram，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "telegram" if service else False

    def is_synologychat(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 SynologyChat 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 synologychat，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "synologychat" if service else False

    def is_slack(self, service: Optional[ServiceInfo] = None, name: Optional[str] = None) -> bool:
        """
        判断指定的消息通知服务是否为 Slack 类型，需要传入 `service` 或 `name` 中的任一参数

        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型为 slack，返回 True；否则返回 False。
        """
        if not service:
            service = self.get_service(name=name)
        return service.type == "slack" if service else False
