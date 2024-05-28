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

    def put(self, message: Any, role: str = "plugin", title: str = None, note: Union[list, dict] = None):
        """
        存消息
        :param message: 消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        :param title: 标题
        :param note: 附件json
        """
        if role in ["system", "plugin"]:
            # 没有标题时获取插件名称
            if role == "plugin" and not title:
                title = "插件通知"
            # 系统通知，默认
            self.sys_queue.put(json.dumps({
                "type": role,
                "title": title,
                "text": message,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "note": note
            }))
        else:
            if isinstance(message, str):
                # 非系统的文本通知
                self.user_queue.put(json.dumps({
                    "title": title,
                    "text": message,
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "note": note
                }))
            elif hasattr(message, "to_dict"):
                # 非系统的复杂结构通知，如媒体信息/种子列表等。
                content = message.to_dict()
                content['title'] = title
                content['date'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                content['note'] = note
                self.user_queue.put(json.dumps(content))

    def get(self, role: str = "system") -> Optional[str]:
        """
        取消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        """
        if role == "system":
            if not self.sys_queue.empty():
                return self.sys_queue.get(block=False)
        else:
            if not self.user_queue.empty():
                return self.user_queue.get(block=False)
        return None
