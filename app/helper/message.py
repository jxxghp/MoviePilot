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

    def put(self, message: Any, role: str = "sys", note: Union[list, dict] = None):
        """
        存消息
        :param message: 消息
        :param role: 消息通道 sys/user
        :param note: 附件json
        """
        if role == "sys":
            self.sys_queue.put(message)
        else:
            if isinstance(message, str):
                self.user_queue.put(message)
            elif hasattr(message, "to_dict"):
                content = message.to_dict()
                content['date'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                content['note'] = json.dumps(note) if note else None
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
