import copy
import importlib
import inspect
import random
import threading
import time
import traceback
import uuid
from functools import lru_cache
from queue import Empty, PriorityQueue
from typing import Callable, Dict, List, Optional, Union

from app.helper.thread import ThreadHelper
from app.log import logger
from app.schemas import ChainEventData
from app.schemas.types import ChainEventType, EventType
from app.utils.limit import ExponentialBackoffRateLimiter
from app.utils.singleton import Singleton

DEFAULT_EVENT_PRIORITY = 10  # 事件的默认优先级
MIN_EVENT_CONSUMER_THREADS = 1  # 最小事件消费者线程数
INITIAL_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 1  # 事件队列空闲时的初始超时时间（秒）
MAX_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 5  # 事件队列空闲时的最大超时时间（秒）


class Event:
    """
    事件类，封装事件的基本信息
    """

    def __init__(self, event_type: Union[EventType, ChainEventType],
                 event_data: Optional[Union[Dict, ChainEventData]] = None,
                 priority: Optional[int] = DEFAULT_EVENT_PRIORITY):
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
        self.__executor = ThreadHelper()  # 动态线程池，用于消费事件
        self.__consumer_threads = []  # 用于保存启动的事件消费者线程
        self.__event_queue = PriorityQueue()  # 优先级队列
        self.__broadcast_subscribers: Dict[EventType, Dict[str, Callable]] = {}  # 广播事件的订阅者
        self.__chain_subscribers: Dict[ChainEventType, Dict[str, tuple[int, Callable]]] = {}  # 链式事件的订阅者
        self.__disabled_handlers = set()  # 禁用的事件处理器集合
        self.__disabled_classes = set()  # 禁用的事件处理器类集合
        self.__lock = threading.Lock()  # 线程锁

    def start(self):
        """
        开始广播事件处理线程
        """
        # 启动消费者线程用于处理广播事件
        self.__event.set()
        for _ in range(MIN_EVENT_CONSUMER_THREADS):
            thread = threading.Thread(target=self.__broadcast_consumer_loop, daemon=True)
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

    def check(self, etype: Union[EventType, ChainEventType]) -> bool:
        """
        检查是否有启用的事件处理器可以响应某个事件类型
        :param etype: 事件类型 (EventType 或 ChainEventType)
        :return: 返回是否存在可用的处理器
        """
        if isinstance(etype, ChainEventType):
            handlers = self.__chain_subscribers.get(etype, {})
            return any(
                self.__is_handler_enabled(handler)
                for _, handler in handlers.values()
            )
        else:
            handlers = self.__broadcast_subscribers.get(etype, {})
            return any(
                self.__is_handler_enabled(handler)
                for handler in handlers.values()
            )

    def send_event(self, etype: Union[EventType, ChainEventType], data: Optional[Union[Dict, ChainEventData]] = None,
                   priority: Optional[int] = DEFAULT_EVENT_PRIORITY) -> Optional[Event]:
        """
        发送事件，根据事件类型决定是广播事件还是链式事件
        :param etype: 事件类型 (EventType 或 ChainEventType)
        :param data: 可选，事件数据
        :param priority: 广播事件的优先级，默认为 10
        :return: 如果是链式事件，返回处理后的事件数据；否则返回 None
        """
        event = Event(etype, data, priority)
        if isinstance(etype, EventType):
            return self.__trigger_broadcast_event(event)
        elif isinstance(etype, ChainEventType):
            return self.__trigger_chain_event(event)
        else:
            logger.error(f"Unknown event type: {etype}")
        return None

    def add_event_listener(self, event_type: Union[EventType, ChainEventType], handler: Callable,
                           priority: Optional[int] = DEFAULT_EVENT_PRIORITY):
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

        def parse_handler_data(data):
            """
            解析处理器数据，判断是否包含优先级
            :param data: 订阅者数据，可能是元组或单一值
            :return: (priority, handler)，若没有优先级则返回 (None, handler)
            """
            if isinstance(data, tuple) and len(data) == 2:
                return data
            return None, data

        handler_info = []
        # 统一处理广播事件和链式事件
        for event_type, subscribers in {**self.__broadcast_subscribers, **self.__chain_subscribers}.items():
            for handler_identifier, handler_data in subscribers.items():
                # 解析优先级和处理器
                priority, handler = parse_handler_data(handler_data)
                # 检查处理器的启用状态
                status = "enabled" if self.__is_handler_enabled(handler) else "disabled"
                # 构建处理器信息字典
                handler_dict = {
                    "event_type": event_type.value,
                    "handler_identifier": handler_identifier,
                    "status": status
                }
                if priority is not None:
                    handler_dict["priority"] = priority
                handler_info.append(handler_dict)
        return handler_info

    @classmethod
    @lru_cache(maxsize=1000)
    def __get_handler_identifier(cls, target: Union[Callable, type]) -> Optional[str]:
        """
        获取处理器或处理器类的唯一标识符，包括模块名和类名/方法名
        :param target: 处理器函数或类
        :return: 唯一标识符
        """
        # 统一使用 inspect.getmodule 来获取模块名
        module = inspect.getmodule(target)
        module_name = module.__name__ if module else "unknown_module"

        # 使用 __qualname__ 获取目标的限定名
        qualname = target.__qualname__
        return f"{module_name}.{qualname}"

    @classmethod
    @lru_cache(maxsize=1000)
    def __get_class_from_callable(cls, handler: Callable) -> Optional[str]:
        """
        获取可调用对象所属类的唯一标识符
        :param handler: 可调用对象（函数、方法等）
        :return: 类的唯一标识符
        """
        # 对于绑定方法，通过 __self__.__class__ 获取类
        if inspect.ismethod(handler) and hasattr(handler, "__self__"):
            return cls.__get_handler_identifier(handler.__self__.__class__)

        # 对于类实例（实现了 __call__ 方法）
        if not inspect.isfunction(handler) and hasattr(handler, "__call__"):
            handler_cls = handler.__class__  # noqa
            return cls.__get_handler_identifier(handler_cls)

        # 对于未绑定方法、静态方法、类方法，使用 __qualname__ 提取类信息
        qualname_parts = handler.__qualname__.split(".")
        if len(qualname_parts) > 1:
            class_name = ".".join(qualname_parts[:-1])
            module = inspect.getmodule(handler)
            module_name = module.__name__ if module else "unknown_module"
            return f"{module_name}.{class_name}"
        return None

    def __is_handler_enabled(self, handler: Callable) -> bool:
        """
        检查处理器是否已启用（没有被禁用）
        :param handler: 处理器函数
        :return: 如果处理器启用则返回 True，否则返回 False
        """
        # 获取处理器的唯一标识符
        handler_id = self.__get_handler_identifier(handler)

        # 获取处理器所属类的唯一标识符
        class_id = self.__get_class_from_callable(handler)

        # 检查处理器或类是否被禁用，只要其中之一被禁用则返回 False
        if handler_id in self.__disabled_handlers or (class_id is not None and class_id in self.__disabled_classes):
            return False

        return True

    def __trigger_chain_event(self, event: Event) -> Optional[Event]:
        """
        触发链式事件，按顺序调用订阅的处理器，并记录处理耗时
        """
        logger.debug(f"Triggering synchronous chain event: {event}")
        dispatch = self.__dispatch_chain_event(event)
        return event if dispatch else None

    def __trigger_broadcast_event(self, event: Event):
        """
        触发广播事件，将事件插入到优先级队列中
        :param event: 要处理的事件对象
        """
        logger.debug(f"Triggering broadcast event: {event}")
        self.__event_queue.put((event.priority, event))

    def __dispatch_chain_event(self, event: Event) -> bool:
        """
        同步方式调度链式事件，按优先级顺序逐个调用事件处理器，并记录每个处理器的处理时间
        :param event: 要调度的事件对象
        """
        handlers = self.__chain_subscribers.get(event.event_type, {})
        if not handlers:
            logger.debug(f"No handlers found for chain event: {event}")
            return False

        # 过滤出启用的处理器
        enabled_handlers = {handler_id: (priority, handler) for handler_id, (priority, handler) in handlers.items()
                            if self.__is_handler_enabled(handler)}

        if not enabled_handlers:
            logger.debug(f"No enabled handlers found for chain event: {event}. Skipping execution.")
            return False

        self.__log_event_lifecycle(event, "Started")
        for handler_id, (priority, handler) in enabled_handlers.items():
            start_time = time.time()
            self.__safe_invoke_handler(handler, event)
            logger.debug(
                f"{self.__get_handler_identifier(handler)} (Priority: {priority}), "
                f"completed in {time.time() - start_time:.3f}s for event: {event}"
            )
        self.__log_event_lifecycle(event, "Completed")
        return True

    def __dispatch_broadcast_event(self, event: Event):
        """
        异步方式调度广播事件，通过线程池逐个调用事件处理器
        :param event: 要调度的事件对象
        """
        handlers = self.__broadcast_subscribers.get(event.event_type, {})
        if not handlers:
            logger.debug(f"No handlers found for broadcast event: {event}")
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
            from app.core.module import ModuleManager

            if class_name in PluginManager().get_plugin_ids():
                def plugin_callable():
                    """
                    插件调用函数
                    """
                    PluginManager().run_plugin_method(class_name, method_name, event_to_process)

                if is_broadcast_event:
                    self.__executor.submit(plugin_callable)
                else:
                    plugin_callable()
            elif class_name in ModuleManager().get_module_ids():
                module = ModuleManager().get_running_module(class_name)
                if module:
                    method = getattr(module, method_name, None)
                    if method:
                        if is_broadcast_event:
                            self.__executor.submit(method, event_to_process)
                        else:
                            method(event_to_process)
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
            try:
                class_obj = globals()[class_name]()
                return class_obj
            except Exception as e:
                logger.error(f"事件处理出错：创建全局类实例出错：{str(e)} - {traceback.format_exc()}")
                return None

        # 如果类不在全局变量中，尝试动态导入模块并创建实例
        try:
            if class_name == "Command":
                module_name = "app.command"
                module = importlib.import_module(module_name)
            elif class_name == "Monitor":
                module_name = "app.monitor"
                module = importlib.import_module(module_name)
            elif class_name == "Scheduler":
                module_name = "app.scheduler"
                module = importlib.import_module(module_name)
            elif class_name == "PluginManager":
                module_name = "app.core.plugin"
                module = importlib.import_module(module_name)
            elif class_name.endswith("Chain"):
                module_name = f"app.chain.{class_name[:-5].lower()}"
                module = importlib.import_module(module_name)
            else:
                logger.debug(f"事件处理出错：不支持的类名: {class_name}")
                return None
            if hasattr(module, class_name):
                class_obj = getattr(module, class_name)()
                return class_obj
            else:
                logger.debug(f"事件处理出错：模块 {module_name} 中没有找到类 {class_name}")
        except Exception as e:
            logger.error(f"事件处理出错：{str(e)} - {traceback.format_exc()}")
        return None

    def __broadcast_consumer_loop(self):
        """
        持续从队列中提取事件的后台广播消费者线程
        """
        jitter_factor = 0.1
        rate_limiter = ExponentialBackoffRateLimiter(base_wait=INITIAL_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS,
                                                     max_wait=MAX_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS,
                                                     backoff_factor=2.0,
                                                     source="BroadcastConsumer",
                                                     enable_logging=False)
        while self.__event.is_set():
            try:
                priority, event = self.__event_queue.get(timeout=rate_limiter.current_wait)
                rate_limiter.reset()
                self.__dispatch_broadcast_event(event)
            except Empty:
                rate_limiter.current_wait = rate_limiter.current_wait * random.uniform(1, 1 + jitter_factor)
                rate_limiter.trigger_limit()

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

        # 发送系统错误通知
        from app.helper.message import MessageHelper
        MessageHelper().put(title=f"{event.event_type} 事件处理出错",
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

    def register(self, etype: Union[EventType, ChainEventType, List[Union[EventType, ChainEventType]], type],
                 priority: Optional[int] = DEFAULT_EVENT_PRIORITY):
        """
        事件注册装饰器，用于将函数注册为事件的处理器
        :param etype:
            - 单个事件类型成员 (如 EventType.MetadataScrape, ChainEventType.PluginAction)
            - 事件类型类 (EventType, ChainEventType)
            - 或事件类型成员的列表
        :param priority: 可选，链式事件的优先级，默认为 DEFAULT_EVENT_PRIORITY
        """

        def decorator(f: Callable):
            # 将输入的事件类型统一转换为列表格式
            if isinstance(etype, list):
                # 传入的已经是列表，直接使用
                event_list = etype
            else:
                # 不是列表则包裹成单一元素的列表
                event_list = [etype]

            # 遍历列表，处理每个事件类型
            for event in event_list:
                if isinstance(event, (EventType, ChainEventType)):
                    self.add_event_listener(event, f, priority)
                elif isinstance(event, type) and issubclass(event, (EventType, ChainEventType)):
                    # 如果是 EventType 或 ChainEventType 类，提取该类中的所有成员
                    for et in event.__members__.values():
                        self.add_event_listener(et, f, priority)
                else:
                    raise ValueError(f"无效的事件类型: {event}")

            return f

        return decorator


# 全局实例定义
eventmanager = EventManager()
