from typing import List, Optional

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import NotificationConf, NotificationSwitchConf
from app.schemas.types import SystemConfigKey, NotificationType


class NotificationHelper:
    """
    消息通知渠道帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_clients(self) -> List[NotificationConf]:
        """
        获取消息通知渠道
        """
        client_confs: List[dict] = self.systemconfig.get(SystemConfigKey.Notifications)
        if not client_confs:
            return []
        return [NotificationConf(**conf) for conf in client_confs]
    
    def get_switchs(self) -> List[NotificationSwitchConf]:
        """
        获取消息通知场景开关
        """
        switchs: List[dict] = self.systemconfig.get(SystemConfigKey.NotificationSwitchs)
        if not switchs:
            return []
        return [NotificationSwitchConf(**switch) for switch in switchs]

    def get_switch(self, mtype: NotificationType) -> Optional[str]:
        """
        获取消息通知场景开关
        """
        switchs = self.get_switchs()
        for switch in switchs:
            if switch.type == mtype.value:
                return switch.action
        return None
