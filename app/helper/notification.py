from typing import List

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import NotificationConf
from app.schemas.types import SystemConfigKey


class NotificationHelper:
    """
    消息通知渠道帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_notifications(self) -> List[NotificationConf]:
        """
        获取消息通知渠道
        """
        notification_confs: List[dict] = self.systemconfig.get(SystemConfigKey.Notifications)
        if not notification_confs:
            return []
        return [NotificationConf(**conf) for conf in notification_confs]
