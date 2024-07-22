import asyncio
import threading
from queue import Queue, Empty
from typing import Dict, Any

from app.log import logger
from app.utils.singleton import Singleton
from app.schemas.types import EventType


class EventManager(metaclass=Singleton):
    """
    事件管理器
    """

    def __init__(self):
        # 事件队列
        self._eventQueue = Queue()
        # 事件响应函数字典
        self._handlers: Dict[str, Dict[str, Any]] = {}
        # 已禁用的事件响应
        self._disabled_handlers = []

    def get_event(self):
        """
        获取事件
        """
        try:
            event = self._eventQueue.get(block=True, timeout=1)
            handlers = self._handlers.get(event.event_type) or {}
            if handlers:
                # 去除掉被禁用的事件响应
                handlerList = [handler for handler in handlers.values()
                               if handler.__qualname__.split(".")[0] not in self._disabled_handlers]
                return event, handlerList
            return event, []
        except Empty:
            return None, []

    def check(self, etype: EventType):
        """
        检查事件是否存在响应，去除掉被禁用的事件响应
        """
        if etype.value not in self._handlers:
            return False
        handlers = self._handlers.get(etype.value)
        return any([handler for handler in handlers.values()
                    if handler.__qualname__.split(".")[0] not in self._disabled_handlers])

    def add_event_listener(self, etype: EventType, handler: type):
        """
        注册事件处理
        """
        try:
            handlers = self._handlers[etype.value]
        except KeyError:
            handlers = {}
            self._handlers[etype.value] = handlers
        if handler.__qualname__ in handlers:
            handlers.pop(handler.__qualname__)
        else:
            logger.debug(f"Event Registed：{etype.value} - {handler.__qualname__}")
        handlers[handler.__qualname__] = handler

    def disable_events_hander(self, class_name: str):
        """
        标记对应类事件处理为不可用
        """
        if class_name not in self._disabled_handlers:
            self._disabled_handlers.append(class_name)
            logger.debug(f"Event Disabled：{class_name}")

    def enable_events_hander(self, class_name: str):
        """
        标记对应类事件处理为可用
        """
        if class_name in self._disabled_handlers:
            self._disabled_handlers.remove(class_name)
        logger.debug(f"Event Enabled：{class_name}")

    def send_event_sync(self, etype: EventType, data: dict = None):
        """
        发送媒体信息并获取格式化后的媒体消息
        """
        result: any = None
        condition = threading.Condition()

        def callback(res):
            nonlocal result
            if res:
                result = res.result()
            with condition:
                condition.notify_all()

        thread = threading.Thread(target=self.send_event, args=(etype, data, callback))
        thread.start()

        with condition:
            condition.wait()

        return result

    def send_event(self, etype: EventType, data: dict = None, callback=None):
        """
        发送事件
        """
        if etype not in EventType:
            return
        event = Event(etype.value)
        event.event_data = data or {}
        event.event_callback = callback
        logger.debug(f"发送事件：{etype.value} - {event.event_data}")
        self._eventQueue.put(event)

    def register(self, etype: [EventType, list]):
        """
        事件注册
        :param etype: 事件类型
        """

        def decorator(f):
            if isinstance(etype, list):
                for et in etype:
                    self.add_event_listener(et, f)
            elif type(etype) == type(EventType):
                for et in etype.__members__.values():
                    self.add_event_listener(et, f)
            else:
                self.add_event_listener(etype, f)
            return f

        return decorator


class Event(object):
    """
    事件对象
    """

    def __init__(self, event_type=None):
        # 事件类型
        self.event_type = event_type
        # 字典用于保存具体的事件数据
        self.event_data = {}
        # 事件的返回结果处理函数
        self.event_callback = None


# 实例引用，用于注册事件
eventmanager = EventManager()
