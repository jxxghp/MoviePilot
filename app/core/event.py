import threading
import uuid
from enum import Enum
from queue import PriorityQueue, Empty
from typing import Callable, Dict, List, Union, Optional

from app.helper.thread import ThreadHelper
from app.log import logger
from app.schemas.types import EventType, SyncEventType
from app.utils.singleton import Singleton


class Event:
    """
    事件类，封装事件的基本信息
    """

    def __init__(self, event_type: Union[EventType, SyncEventType], event_data: Optional[Dict] = None,
                 priority: int = 10):
        """
        :param event_type: 事件的类型，支持 EventType 或 SyncEventType
        :param event_data: 可选，事件携带的数据，默认为空字典
        :param priority: 可选，广播事件的优先级，默认为 10
        """
        self.event_id = str(uuid.uuid4())  # 事件ID
        self.event_type = event_type  # 事件类型
        self.event_data = event_data or {}  # 事件数据
        self.priority = priority  # 事件优先级

    def __repr__(self) -> str:
        """
        重写 __repr__ 方法，用于返回事件的详细信息，包括事件类型、事件ID和优先级
        """
        event_kind = Event.get_event_kind(self.event_type)
        return f"<{event_kind}: {self.event_type.value}, ID: {self.event_id}, Priority: {self.priority}>"

    @staticmethod
    def get_event_kind(event_type: Union[EventType, SyncEventType]) -> str:
        """
        根据事件类型判断事件是广播事件还是链式事件
        :param event_type: 事件类型，支持 EventType 或 SyncEventType
        :return: 返回 Broadcast Event 或 Chain Event
        """
        return "Broadcast Event" if isinstance(event_type, EventType) else "Chain Event"


class EventManager(metaclass=Singleton):
    """
    EventManager 负责管理和调度广播事件和链式事件，包括订阅、发送和处理事件
    """

    def __init__(self, max_workers: int = 50):
        """
        :param max_workers: 线程池最大工作线程数，默认 50
        """
        self.__executor = ThreadHelper(max_workers=max_workers)  # 动态线程池，用于消费事件
        self.__event_executor = ThreadHelper(max_workers=3)  # 动态线程池，用于处理事件
        self.__event_queue = PriorityQueue()  # 优先级队列
        self.__subscribers: Dict[Union[EventType, SyncEventType], List[Callable[[Dict], None]]] = {}  # 订阅者列表
        self.__disabled_handlers = set()  # 禁用的事件处理器集合
        self.__lock = threading.Lock()  # 线程锁
        self.__dynamic_consuming = False  # 标记是否已经在使用动态线程池

        # 启动消费者线程用于处理异步事件
        threading.Thread(target=self.__fixed_consumer, daemon=True).start()

    def send_event(self, etype: Union[EventType, SyncEventType], data: Optional[Dict] = None, priority: int = 10) -> \
            Optional[Dict]:
        """
        发送事件，根据事件类型决定是广播事件还是链式事件
        :param etype: 事件类型 (EventType 或 SyncEventType)
        :param data: 可选，事件数据
        :param priority: 广播事件的优先级，默认为 10
        :return: 如果是链式事件，返回处理后的事件数据；否则返回 None
        """
        event = Event(etype, data, priority if isinstance(etype, EventType) else None)
        if isinstance(etype, EventType):
            self.__trigger_event_async(event, priority)
        elif isinstance(etype, SyncEventType):
            return self.__trigger_event(event)
        else:
            logger.error(f"Unknown event type: {etype}")

    def add_event_listener(self, event_type: Union[EventType, SyncEventType], handler: Callable[[Dict], None]) -> None:
        """
        注册事件处理器，将处理器添加到对应的事件订阅列表中
        :param event_type: 事件类型 (EventType 或 SyncEventType)
        :param handler: 处理器
        """
        with self.__lock:
            if event_type not in self.__subscribers:
                self.__subscribers[event_type] = []
            self.__subscribers[event_type].append(handler)
            event_kind = Event.get_event_kind(event_type)
            logger.debug(f"Subscribed to event: {event_type.value} ({event_kind}), Handler: {handler.__name__}")

    def remove_event_listener(self, event_type: Union[EventType, SyncEventType],
                              handler: Callable[[Dict], None]) -> None:
        """
        移除事件处理器，将处理器从对应事件的订阅列表中删除
        :param event_type: 事件类型 (EventType 或 SyncEventType)
        :param handler: 要移除的处理器
        """
        with self.__lock:
            if event_type in self.__subscribers:
                self.__subscribers[event_type].remove(handler)
                event_kind = Event.get_event_kind(event_type)
                logger.debug(f"Unsubscribed from event: {event_type.value} ({event_kind}), Handler: {handler.__name__}")

    def disable_event_handler(self, handler_name: str) -> None:
        """
        禁用指定名称的事件处理器，防止其响应事件
        :param handler_name: 要禁用的事件处理器名称
        """
        self.__disabled_handlers.add(handler_name)
        logger.debug(f"Disabled event handler: {handler_name}")

    def enable_event_handler(self, handler_name: str) -> None:
        """
        启用指定名称的事件处理器，使其可以继续响应事件
        :param handler_name: 要启用的事件处理器名称
        """
        self.__disabled_handlers.discard(handler_name)
        logger.debug(f"Enabled event handler: {handler_name}")

    def check(self, etype: Union[EventType, SyncEventType]) -> bool:
        """
        检查是否有启用的事件处理器可以响应某个事件类型
        :param etype: 事件类型 (EventType 或 SyncEventType)
        :return: 返回是否存在可用的处理器
        """
        if etype not in self.__subscribers:
            return False
        handlers = self.__subscribers.get(etype, [])
        return any(handler.__name__ not in self.__disabled_handlers for handler in handlers)

    def __trigger_event(self, event: Event) -> Dict:
        """
        触发链式事件，按顺序调用订阅的处理器
        :param event: 要处理的事件对象
        :return: 返回处理后的事件数据
        """
        logger.debug(f"Triggering synchronous chain event: {event}")
        self.__dispatch_event(event)
        return event.event_data

    def __trigger_event_async(self, event: Event, priority: int) -> None:
        """
        触发广播事件，将事件插入到优先级队列中
        :param event: 要处理的事件对象
        :param priority: 事件的优先级
        """
        logger.debug(f"Triggering asynchronous broadcast event: {event}")
        self.__event_queue.put((priority, event))

        # 当固定消费者无法及时处理时，动态启动线程池
        if self.__event_queue.qsize() > 10 and not self.__dynamic_consuming:
            self.__dynamic_consuming = True
            self.__event_executor.submit(self.__dynamic_consumer)

    def __dispatch_event(self, event: Event) -> None:
        """
        同步方式调度事件，逐个调用事件处理器
        :param event: 要调度的事件对象
        """
        handlers = self.__subscribers.get(event.event_type, [])
        for handler in handlers:
            if handler.__name__ not in self.__disabled_handlers:
                handler(event.event_data)

    def __dispatch_event_async(self, event: Event) -> None:
        """
        异步方式调度事件，通过线程池逐个调用事件处理器
        :param event: 要调度的事件对象
        """
        handlers = self.__subscribers.get(event.event_type, [])
        for handler in handlers:
            if handler.__name__ not in self.__disabled_handlers:
                self.__executor.submit(handler, event.event_data)

    def __fixed_consumer(self) -> None:
        """
        固定的后台消费者线程，持续从队列中提取事件处理
        该线程始终保持运行状态，确保即使事件量少时也有线程在消费
        """
        while True:
            try:
                # 阻塞方式从队列获取事件
                priority, event = self.__event_queue.get(block=True, timeout=1)
                logger.debug(f"Fixed consumer processing event: {event}")
                self.__dispatch_event_async(event)  # 调用事件处理器
            except Empty:
                continue  # 如果队列为空，继续等待

    def __dynamic_consumer(self) -> None:
        """
        动态消费者线程，通过线程池调度，用于在事件量大时进行扩展
        一旦队列为空，则结束动态消费，并重置动态消费标志
        """
        while True:
            try:
                # 非阻塞方式从队列获取事件
                priority, event = self.__event_queue.get(block=False)
                logger.debug(f"Dynamic consumer processing event: {event}")
                self.__dispatch_event_async(event)  # 调用事件处理器
            except Empty:
                self.__dynamic_consuming = False  # 队列为空，结束动态消费
                break

    def register(self, etype: Union[EventType, SyncEventType, List[Union[EventType, SyncEventType]], type]):
        """
        事件注册装饰器，用于将函数注册为事件的处理器
        :param etype:
            - 单个事件类型成员 (如 EventType.MetadataScrape, SyncEventType.PluginAction)
            - 事件类型类 (EventType, SyncEventType)
            - 或事件类型成员的列表
        """

        def decorator(f: Callable[[Dict], None]):
            event_list = []

            # 如果传入的是列表，处理每个事件类型
            if isinstance(etype, list):
                for et in etype:
                    if isinstance(et, (EventType, SyncEventType)):
                        event_list.append(et)
                    else:
                        raise ValueError(f"列表中无效的事件类型: {et}")

            # 如果传入的是 EventType 或 SyncEventType 类，提取该类中的所有成员
            elif isinstance(etype, type) and issubclass(etype, Enum):
                event_list.extend(etype.__members__.values())

            # 如果传入的是单个事件类型成员 (EventType.MetadataScrape 或 SyncEventType.PluginAction)
            elif isinstance(etype, (EventType, SyncEventType)):
                event_list.append(etype)

            else:
                raise ValueError(f"无效的事件类型: {etype}")

            # 统一注册事件
            for event in event_list:
                self.add_event_listener(event, f)

            return f

        return decorator


# 全局实例定义
eventmanager = EventManager()
