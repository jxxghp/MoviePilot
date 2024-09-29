from app.helper.servicebase import ServiceBaseHelper
from app.schemas import NotificationConf
from app.schemas.types import SystemConfigKey


class NotificationHelper(ServiceBaseHelper[NotificationConf]):
    """
    消息通知帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.Notifications,
            conf_type=NotificationConf,
            modules=["WechatModule", "WebPushModule", "VoceChatModule", "TelegramModule", "SynologyChatModule",
                     "SlackModule"]
        )
