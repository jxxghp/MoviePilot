from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class NotificationHelper:
    """
    消息通知渠道帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_notifications(self) -> dict:
        """
        获取消息通知渠道
        """
        notification_conf: dict = self.systemconfig.get(SystemConfigKey.Notifications)
        if not notification_conf:
            return {}
        return notification_conf
