import json
import queue
import time
from typing import Optional, Any, Union

from app.utils.singleton import Singleton


class MessageHelper(metaclass=Singleton):
    """
    消息队列管理器，包括系统消息和用户消息
    """
    def __init__(self):
        self.sys_queue = queue.Queue()
        self.user_queue = queue.Queue()

    def put(self, message: Any, role: str = "system", note: Union[list, dict] = None):
        """
        存消息
        :param message: 消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        :param note: 附件json
        """
        if role in ["system", "plugin"]:
            # 系统通知，默认
            self.sys_queue.put(json.dumps({
                "type": role,
                "title": message,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "text": note
            }))
        else:
            if isinstance(message, str):
                # 非系统的文本通知
                self.user_queue.put(json.dumps({
                    "title": message,
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "note": note
                }))
            elif hasattr(message, "to_dict"):
                # 非系统的复杂结构通知，如媒体信息/种子列表等。
                content = message.to_dict()
                content['date'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                content['note'] = note
                self.user_queue.put(json.dumps(content))

    def get(self, role: str = "sys") -> Optional[str]:
        """
        取消息
        :param role: 消息通道 sys/user
        """
        if role == "sys":
            if not self.sys_queue.empty():
                return self.sys_queue.get(block=False)
        else:
            if not self.user_queue.empty():
                return self.user_queue.get(block=False)
        return None
