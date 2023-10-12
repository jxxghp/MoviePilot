from queue import Queue, Empty

from app.log import logger
from app.utils.singleton import Singleton
from app.schemas.types import EventType


class EventManager(metaclass=Singleton):
    """
    事件管理器
    """

    # 事件队列
    _eventQueue: Queue = None
    # 事件响应函数字典
    _handlers: dict = {}

    def __init__(self):
        # 事件队列
        self._eventQueue = Queue()
        # 事件响应函数字典
        self._handlers = {}

    def get_event(self):
        """
        获取事件
        """
        try:
            event = self._eventQueue.get(block=True, timeout=1)
            handlerList = self._handlers.get(event.event_type)
            return event, handlerList or []
        except Empty:
            return None, []

    def check(self, etype: EventType):
        """
        检查事件是否存在响应
        """
        return etype.value in self._handlers

    def add_event_listener(self, etype: EventType, handler: type):
        """
        注册事件处理
        """
        try:
            handlerList = self._handlers[etype.value]
        except KeyError:
            handlerList = []
            self._handlers[etype.value] = handlerList
        if handler not in handlerList:
            handlerList.append(handler)
            logger.debug(f"Event Registed：{etype.value} - {handler}")

    def remove_event_listener(self, etype: EventType, handler: type):
        """
        移除监听器的处理函数
        """
        try:
            handlerList = self._handlers[etype.value]
            if handler in handlerList[:]:
                handlerList.remove(handler)
            if not handlerList:
                del self._handlers[etype.value]
        except KeyError:
            pass

    def send_event(self, etype: EventType, data: dict = None):
        """
        发送事件
        """
        if etype not in EventType:
            return
        event = Event(etype.value)
        event.event_data = data or {}
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


# 实例引用，用于注册事件
eventmanager = EventManager()
