import copy
import importlib
import inspect
import threading
import time
import traceback
import uuid
from queue import PriorityQueue, Empty
from typing import Callable, Dict, List, Union, Optional

from app.helper.message import MessageHelper
from app.helper.thread import ThreadHelper
from app.log import logger
from app.schemas.types import EventType, ChainEventType
from app.utils.singleton import Singleton

DEFAULT_EVENT_PRIORITY = 10  # 事件的默认优先级
MIN_EVENT_CONSUMER_THREADS = 1  # 最小事件消费者线程数
EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 30  # 事件队列空闲时的超时时间（秒）


class Event:
    """
    事件类，封装事件的基本信息
    """

    def __init__(self, event_type: Union[EventType, ChainEventType], event_data: Optional[Dict] = None,
                 priority: int = DEFAULT_EVENT_PRIORITY):
        """
        :param event_type: 事件的类型，支持 EventType 或 ChainEventType
        :param event_data: 可选，事件携带的数据，默认为空字典
        :param priority: 可选，事件的优先级，默认为 10
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

    def __lt__(self, other):
        """
        定义事件对象的比较规则，基于优先级比较
        优先级小的事件会被认为“更小”，优先级高的事件将被认为“更大”
        """
        return self.priority < other.priority

    @staticmethod
    def get_event_kind(event_type: Union[EventType, ChainEventType]) -> str:
        """
        根据事件类型判断事件是广播事件还是链式事件
        :param event_type: 事件类型，支持 EventType 或 ChainEventType
        :return: 返回 Broadcast Event 或 Chain Event
        """
        return "Broadcast Event" if isinstance(event_type, EventType) else "Chain Event"


class EventManager(metaclass=Singleton):
    """
    EventManager 负责管理和调度广播事件和链式事件，包括订阅、发送和处理事件
    """

    # 退出事件
    __event = threading.Event()

    def __init__(self):
        self.__messagehelper = MessageHelper()
        self.__executor = ThreadHelper()  # 动态线程池，用于消费事件
        self.__consumer_threads = []  # 用于保存启动的事件消费者线程
        self.__event_queue = PriorityQueue()  # 优先级队列
        self.__broadcast_subscribers: Dict[EventType, Dict[str, Callable]] = {}  # 广播事件的订阅者
        self.__chain_subscribers: Dict[ChainEventType, Dict[str, tuple[int, Callable]]] = {}  # 链式事件的订阅者
        self.__disabled_handlers = set()  # 禁用的事件处理器集合
        self.__disabled_classes = set()  # 禁用的事件处理器类集合
        self.__lock = threading.Lock()  # 线程锁
        self.__condition = threading.Condition(self.__lock)  # 条件变量

    def start(self):
        """
        开始广播事件处理线程
        """
        # 启动消费者线程用于处理广播事件
        self.__event.set()
        for _ in range(MIN_EVENT_CONSUMER_THREADS):
            thread = threading.Thread(target=self.__fixed_broadcast_consumer, daemon=True)
            thread.start()
            self.__consumer_threads.append(thread)  # 将线程对象保存到列表中

    def stop(self):
        """
        停止广播事件处理线程
        """
        logger.info("正在停止事件处理...")
        self.__event.clear()  # 停止广播事件处理
        try:
            # 通过遍历保存的线程来等待它们完成
            for consumer_thread in self.__consumer_threads:
                consumer_thread.join()
            logger.info("事件处理停止完成")
        except Exception as e:
            logger.error(f"停止事件处理线程出错：{str(e)} - {traceback.format_exc()}")

    def check(self, etype: EventType):
        """
        检查事件是否存在响应，去除掉被禁用的事件响应
        """
        if etype not in self.__broadcast_subscribers:
            return False
        handlers = self.__broadcast_subscribers[etype]
        return any([handler for handler in handlers.values()
                    if handler.__qualname__.split(".")[0] not in self.__disabled_handlers])

    def send_event(self, etype: Union[EventType, ChainEventType], data: Optional[Dict] = None,
                   priority: int = DEFAULT_EVENT_PRIORITY) -> Optional[Event]:
        """
        发送事件，根据事件类型决定是广播事件还是链式事件
        :param etype: 事件类型 (EventType 或 ChainEventType)
        :param data: 可选，事件数据
        :param priority: 广播事件的优先级，默认为 10
        :return: 如果是链式事件，返回处理后的事件数据；否则返回 None
        """
        event = Event(etype, data, priority)
        if isinstance(etype, EventType):
            self.__trigger_broadcast_event(event)
            with self.__condition:
                self.__condition.notify()
        elif isinstance(etype, ChainEventType):
            return self.__trigger_chain_event(event)
        else:
            logger.error(f"Unknown event type: {etype}")

    def add_event_listener(self, event_type: Union[EventType, ChainEventType], handler: Callable,
                           priority: int = DEFAULT_EVENT_PRIORITY):
        """
        注册事件处理器，将处理器添加到对应的事件订阅列表中
        :param event_type: 事件类型 (EventType 或 ChainEventType)
        :param handler: 处理器
        :param priority: 可选，链式事件的优先级，默认为 10；广播事件不需要优先级
        """
        with self.__lock:
            handler_identifier = self.__get_handler_identifier(handler)

            if isinstance(event_type, ChainEventType):
                # 链式事件，按优先级排序
                if event_type not in self.__chain_subscribers:
                    self.__chain_subscribers[event_type] = {}
                handlers = self.__chain_subscribers[event_type]
                if handler_identifier in handlers:
                    handlers.pop(handler_identifier)
                else:
                    logger.debug(
                        f"Subscribed to chain event: {event_type.value}, "
                        f"Priority: {priority} - {handler_identifier}")
                handlers[handler_identifier] = (priority, handler)
                # 根据优先级排序
                self.__chain_subscribers[event_type] = dict(
                    sorted(self.__chain_subscribers[event_type].items(), key=lambda x: x[1][0])
                )
            else:
                # 广播事件
                if event_type not in self.__broadcast_subscribers:
                    self.__broadcast_subscribers[event_type] = {}
                handlers = self.__broadcast_subscribers[event_type]
                if handler_identifier in handlers:
                    handlers.pop(handler_identifier)
                else:
                    logger.debug(f"Subscribed to broadcast event: {event_type.value} - {handler_identifier}")
                handlers[handler_identifier] = handler

    def remove_event_listener(self, event_type: Union[EventType, ChainEventType], handler: Callable):
        """
        移除事件处理器，将处理器从对应事件的订阅列表中删除
        :param event_type: 事件类型 (EventType 或 ChainEventType)
        :param handler: 要移除的处理器
        """
        with self.__lock:
            handler_identifier = self.__get_handler_identifier(handler)

            if isinstance(event_type, ChainEventType) and event_type in self.__chain_subscribers:
                self.__chain_subscribers[event_type].pop(handler_identifier, None)
                logger.debug(f"Unsubscribed from chain event: {event_type.value} - {handler_identifier}")
            elif event_type in self.__broadcast_subscribers:
                self.__broadcast_subscribers[event_type].pop(handler_identifier, None)
                logger.debug(f"Unsubscribed from broadcast event: {event_type.value} - {handler_identifier}")

    def disable_event_handler(self, target: Union[Callable, type]):
        """
        禁用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        """
        identifier = self.__get_handler_identifier(target)
        if identifier in self.__disabled_handlers or identifier in self.__disabled_classes:
            return
        if isinstance(target, type):
            self.__disabled_classes.add(identifier)
            logger.debug(f"Disabled event handler class - {identifier}")
        else:
            self.__disabled_handlers.add(identifier)
            logger.debug(f"Disabled event handler - {identifier}")

    def enable_event_handler(self, target: Union[Callable, type]):
        """
        启用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        """
        identifier = self.__get_handler_identifier(target)
        if isinstance(target, type):
            self.__disabled_classes.discard(identifier)
            logger.debug(f"Enabled event handler class - {identifier}")
        else:
            self.__disabled_handlers.discard(identifier)
            logger.debug(f"Enabled event handler - {identifier}")

    def visualize_handlers(self) -> List[Dict]:
        """
        可视化所有事件处理器，包括是否被禁用的状态
        :return: 处理器列表，包含事件类型、处理器标识符、优先级（如果有）和状态
        """
        handler_info = []
        # 统一处理广播事件和链式事件
        for event_type, subscribers in {**self.__broadcast_subscribers, **self.__chain_subscribers}.items():
            for handler_data in subscribers:
                if isinstance(subscribers, dict):
                    priority, handler = handler_data
                else:
                    priority = None
                    handler = handler_data
                # 获取处理器的唯一标识符
                handler_id = self.__get_handler_identifier(handler)
                # 检查处理器的启用状态
                status = "enabled" if self.__is_handler_enabled(handler) else "disabled"
                # 构建处理器信息字典
                handler_dict = {
                    "event_type": event_type.value,
                    "handler_identifier": handler_id,
                    "status": status
                }
                if priority is not None:
                    handler_dict["priority"] = priority
                handler_info.append(handler_dict)
        return handler_info

    @staticmethod
    def __get_handler_identifier(target: Union[Callable, type]) -> str:
        """
        获取处理器或处理器类的唯一标识符，包括模块名和类名
        :param target: 处理器函数或类
        :return: 唯一标识符
        """
        if isinstance(target, type):
            # 如果是类，使用模块名和类名
            module_name = target.__module__
            class_name = target.__qualname__
            return f"{module_name}.{class_name}"
        else:
            # 如果是函数或方法，使用 inspect.getmodule 来获取模块名
            module = inspect.getmodule(target)
            module_name = module.__name__ if module else "unknown_module"
            qualname = target.__qualname__
            return f"{module_name}.{qualname}"

    def __is_handler_enabled(self, handler: Callable) -> bool:
        """
        检查处理器是否已启用（没有被禁用）
        :param handler: 处理器函数
        :return: 如果处理器启用则返回 True，否则返回 False
        """
        # 获取处理器的唯一标识符
        handler_id = self.__get_handler_identifier(handler)

        # 获取处理器所属类的唯一标识符
        class_id = self.__get_handler_identifier(handler.__self__.__class__) if hasattr(handler, '__self__') else None

        # 检查处理器或类是否被禁用，只要其中之一被禁用则返回 False
        if handler_id in self.__disabled_handlers or (class_id is not None and class_id in self.__disabled_classes):
            return False

        return True

    def __trigger_chain_event(self, event: Event) -> Event:
        """
        触发链式事件，按顺序调用订阅的处理器，并记录处理耗时
        """
        logger.debug(f"Triggering synchronous chain event: {event}")
        self.__dispatch_chain_event(event)
        return event

    def __trigger_broadcast_event(self, event: Event):
        """
        触发广播事件，将事件插入到优先级队列中
        :param event: 要处理的事件对象
        """
        logger.debug(f"Triggering broadcast event: {event}")
        self.__event_queue.put((event.priority, event))

    def __dispatch_chain_event(self, event: Event):
        """
        同步方式调度链式事件，按优先级顺序逐个调用事件处理器，并记录每个处理器的处理时间
        :param event: 要调度的事件对象
        """
        handlers = self.__chain_subscribers.get(event.event_type, {})
        if not handlers:
            return
        self.__log_event_lifecycle(event, "started")
        for handler_id, (priority, handler) in handlers.items():
            start_time = time.time()
            self.__safe_invoke_handler(handler, event)
            logger.debug(
                f"Handler {self.__get_handler_identifier(handler)} (Priority: {priority}) ,"
                f" completed in {time.time() - start_time:.3f}s"
            )
        self.__log_event_lifecycle(event, "completed")

    def __dispatch_broadcast_event(self, event: Event):
        """
        异步方式调度广播事件，通过线程池逐个调用事件处理器
        :param event: 要调度的事件对象
        """
        handlers = self.__broadcast_subscribers.get(event.event_type, {})
        if not handlers:
            return
        for handler_id, handler in handlers.items():
            self.__executor.submit(self.__safe_invoke_handler, handler, event)

    def __safe_invoke_handler(self, handler: Callable, event: Event):
        """
        调用处理器，处理链式或广播事件
        :param handler: 处理器
        :param event: 事件对象
        """
        if not self.__is_handler_enabled(handler):
            logger.debug(f"Handler {self.__get_handler_identifier(handler)} is disabled. Skipping execution")
            return

        # 根据事件类型判断是否需要深复制
        is_broadcast_event = isinstance(event.event_type, EventType)
        event_to_process = copy.deepcopy(event) if is_broadcast_event else event

        names = handler.__qualname__.split(".")
        class_name, method_name = names[0], names[1]

        try:
            from app.core.plugin import PluginManager

            if class_name in PluginManager().get_plugin_ids():
                # 定义一个插件调用函数
                def plugin_callable():
                    PluginManager().run_plugin_method(class_name, method_name, event_to_process)

                if is_broadcast_event:
                    self.__executor.submit(plugin_callable)
                else:
                    plugin_callable()
            else:
                # 获取全局对象或模块类的实例
                class_obj = self.__get_class_instance(class_name)
                if class_obj and hasattr(class_obj, method_name):
                    method = getattr(class_obj, method_name)
                    if is_broadcast_event:
                        self.__executor.submit(method, event_to_process)
                    else:
                        method(event_to_process)
        except Exception as e:
            self.__handle_event_error(event, handler, e)

    @staticmethod
    def __get_class_instance(class_name: str):
        """
        根据类名获取类实例，首先检查全局变量中是否存在该类，如果不存在则尝试动态导入模块。
        :param class_name: 类的名称
        :return: 类的实例
        """
        # 检查类是否在全局变量中
        if class_name in globals():
            class_obj = globals()[class_name]()
        else:
            # 如果类不在全局变量中，尝试动态导入模块并创建实例
            # 导入模块，除了插件和Command，只有chain能响应事件
            try:
                module = importlib.import_module(f"app.chain.{class_name[:-5].lower()}")
                class_obj = getattr(module, class_name)()
            except Exception as e:
                logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")
                return None

        return class_obj

    def __fixed_broadcast_consumer(self):
        """
        固定的后台广播消费者线程，持续从队列中提取事件
        """
        while self.__event.is_set():
            # 使用 Condition 优化队列的等待机制，避免频繁触发超时
            with self.__condition:
                # 阻塞等待，直到有事件插入
                self.__condition.wait()
                try:
                    priority, event = self.__event_queue.get(timeout=EVENT_QUEUE_IDLE_TIMEOUT_SECONDS)
                    self.__dispatch_broadcast_event(event)
                except Empty:
                    logger.debug("Queue is empty, waiting for new events")

    @staticmethod
    def __log_event_lifecycle(event: Event, stage: str):
        """
        记录事件的生命周期日志
        """
        logger.debug(f"{stage} - {event}")

    def __handle_event_error(self, event: Event, handler: Callable, e: Exception):
        """
        全局错误处理器，用于处理事件处理中的异常
        """
        logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")

        names = handler.__qualname__.split(".")
        class_name, method_name = names[0], names[1]

        self.__messagehelper.put(title=f"{event.event_type} 事件处理出错",
                                 message=f"{class_name}.{method_name}：{str(e)}",
                                 role="system")
        self.send_event(
            EventType.SystemError,
            {
                "type": "event",
                "event_type": event.event_type,
                "event_handle": f"{class_name}.{method_name}",
                "error": str(e),
                "traceback": traceback.format_exc()
            }
        )

    def register(self, etype: Union[EventType, ChainEventType, List[Union[EventType, ChainEventType]], type]):
        """
        事件注册装饰器，用于将函数注册为事件的处理器
        :param etype:
            - 单个事件类型成员 (如 EventType.MetadataScrape, ChainEventType.PluginAction)
            - 事件类型类 (EventType, ChainEventType)
            - 或事件类型成员的列表
        """

        def decorator(f: Callable):
            # 将输入的事件类型统一转换为列表格式
            if isinstance(etype, list):
                event_list = etype  # 传入的已经是列表，直接使用
            else:
                event_list = [etype]  # 不是列表则包裹成单一元素的列表

            # 遍历列表，处理每个事件类型
            for event in event_list:
                if isinstance(event, (EventType, ChainEventType)):
                    self.add_event_listener(event, f)
                elif isinstance(event, type) and issubclass(event, (EventType, ChainEventType)):
                    # 如果是 EventType 或 ChainEventType 类，提取该类中的所有成员
                    for et in event.__members__.values():
                        self.add_event_listener(et, f)
                else:
                    raise ValueError(f"无效的事件类型: {event}")

            return f

        return decorator


# 全局实例定义
eventmanager = EventManager()
