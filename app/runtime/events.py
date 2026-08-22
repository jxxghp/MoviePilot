import random
import threading
import traceback
import uuid
from queue import Empty, PriorityQueue
from typing import Callable, Dict, List, Optional, Tuple, Union, Any, Type

from app.runtime.config import global_vars
from app.runtime.thread import ThreadHelper
from app.runtime.log import logger
from app.schemas.event import ChainEventData
from app.schemas.types import ChainEventType, EventType
from app.runtime.rate import ExponentialBackoffRateLimiter
from app.foundation.singleton import Singleton
from app.runtime.event.binding import (
    EventBindingResolver,
    EventHandlerBinding,
    HandlerInstanceResolver,
)
from app.runtime.event.dispatch import EventDispatcher
from app.runtime.event.errors import EventErrorNotifier, EventErrorPolicy
from app.runtime.event.registry import EventRegistry
from app.runtime.event.contracts import normalize_event_type, validate_event_payload
from app.runtime.correlation import get_correlation_id
from app.runtime.observability import record_metric

DEFAULT_EVENT_PRIORITY = 10  # 事件的默认优先级
MIN_EVENT_CONSUMER_THREADS = 1  # 最小事件消费者线程数
INITIAL_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 1  # 事件队列空闲时的初始超时时间（秒）
MAX_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 5  # 事件队列空闲时的最大超时时间（秒）


class Event:
    """
    事件类，封装事件的基本信息
    """

    def __init__(self, event_type: Union[EventType, ChainEventType, str],
                 event_data: Optional[Union[Dict, ChainEventData]] = None,
                 priority: Optional[int] = DEFAULT_EVENT_PRIORITY,
                 correlation_id: Optional[str] = None):
        """
        :param event_type: 事件的类型，支持 EventType 或 ChainEventType
        :param event_data: 可选，事件携带的数据，默认为空字典
        :param priority: 可选，事件的优先级，默认为 10
        :param correlation_id: 生产事件时固化的请求关联 ID
        """
        event_type = normalize_event_type(event_type)
        payload_problems = validate_event_payload(event_type, event_data)
        if payload_problems:
            logger.warning(
                "事件 %s payload 与登记契约不一致：%s；当前保留旧 payload 继续投递",
                getattr(event_type, "value", event_type),
                "; ".join(payload_problems),
            )
        self.event_id = str(uuid.uuid4())  # 事件ID
        self.event_type = event_type  # 事件类型
        self.event_data = event_data or {}  # 事件数据
        self.priority = priority  # 事件优先级
        self.correlation_id = correlation_id or get_correlation_id()

    def __repr__(self) -> str:
        """
        重写 __repr__ 方法，用于返回事件的详细信息，包括事件类型、事件ID和优先级
        """
        event_kind = Event.get_event_kind(self.event_type)
        event_name = getattr(self.event_type, "value", self.event_type)
        return f"<{event_kind}: {event_name}, ID: {self.event_id}, Priority: {self.priority}>"

    def __lt__(self, other):
        """
        定义事件对象的比较规则，基于优先级比较
        优先级小的事件会被认为“更小”，优先级高的事件将被认为“更大”
        """
        return self.priority < other.priority

    @staticmethod
    def get_event_kind(event_type: Union[EventType, ChainEventType, str]) -> str:
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

    def __init__(self):
        """初始化订阅表、处理队列、解析器和消费者状态。"""
        # 动态线程池，用于消费事件
        self.__executor = ThreadHelper()
        # 用于保存启动的事件消费者线程
        self.__consumer_threads = []
        # 优先级队列
        self.__event_queue = PriorityQueue()
        # 广播事件的订阅者
        self.__broadcast_subscribers: Dict[EventType, Dict[str, Callable]] = {}
        # 链式事件的订阅者
        self.__chain_subscribers: Dict[ChainEventType, Dict[str, tuple[int, Callable]]] = {}
        # 禁用的事件处理器集合
        self.__disabled_handlers = set()
        # 禁用的事件处理器类集合
        self.__disabled_classes = set()
        # 禁用的事件处理器运行实例集合，元素为 (类标识, 实例键)
        self.__disabled_instances = set()
        # 线程锁
        self.__lock = threading.Lock()
        # 退出事件
        self.__event = threading.Event()
        # 由上层管理器注册的处理器实例解析器
        self.__handler_instance_resolvers: Dict[str, HandlerInstanceResolver] = {}
        # 由启动组合层注入的错误通知回调
        self.__error_notifier: Optional[EventErrorNotifier] = None
        self.__registry = EventRegistry(
            lock=self.__lock,
            broadcast_subscribers=lambda: self.__broadcast_subscribers,
            chain_subscribers=lambda: self.__chain_subscribers,
            disabled_handlers=lambda: self.__disabled_handlers,
            disabled_classes=lambda: self.__disabled_classes,
            disabled_instances=lambda: self.__disabled_instances,
        )
        self.__binding_resolver = EventBindingResolver(
            lock=self.__lock,
            resolvers=lambda: self.__handler_instance_resolvers,
            instance_enabled=lambda owner, key: self.__registry.is_instance_enabled(
                owner,
                key,
            ),
        )
        self.__error_policy = EventErrorPolicy(
            notifier=lambda: self.__error_notifier,
            emit_system_error=lambda payload: self.send_event(
                EventType.SystemError,
                payload,
            ),
        )
        self.__dispatcher = EventDispatcher(
            registry=self.__registry,
            binding_resolver=self.__binding_resolver,
            executor=lambda: self.__executor,
            event_loop=lambda: global_vars.loop,
            event_factory=Event,
            error_handler=lambda **kwargs: self.__handle_event_error(**kwargs),
        )

    def register_handler_instance_resolver(
            self,
            name: str,
            resolver: HandlerInstanceResolver,
    ) -> None:
        """
        注册上层运行时的事件处理器实例解析器。

        同名解析器会被替换，避免测试重建单例或热重载后保留旧实例引用。
        """
        self.__binding_resolver.register(name, resolver)

    def unresolved_handler_bindings(self) -> tuple[str, ...]:
        """返回未命中显式 resolver 的类处理器诊断清单。"""
        return self.__binding_resolver.unresolved_handlers()

    def set_error_notifier(self, notifier: Optional[EventErrorNotifier]) -> None:
        """设置事件处理异常的外部通知回调。"""
        with self.__lock:
            self.__error_notifier = notifier

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
        return self.__registry.check(etype)

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

    async def async_send_event(self, etype: Union[EventType, ChainEventType],
                               data: Optional[Union[Dict, ChainEventData]] = None,
                               priority: Optional[int] = DEFAULT_EVENT_PRIORITY) -> Optional[Event]:
        """
        异步发送事件，根据事件类型决定是广播事件还是链式事件
        :param etype: 事件类型 (EventType 或 ChainEventType)
        :param data: 可选，事件数据
        :param priority: 广播事件的优先级，默认为 10
        :return: 如果是链式事件，返回处理后的事件数据；否则返回 None
        """
        event = Event(etype, data, priority)
        if isinstance(etype, EventType):
            return self.__trigger_broadcast_event(event)
        elif isinstance(etype, ChainEventType):
            return await self.__trigger_chain_event_async(event)
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
        self.__registry.add(event_type, handler, priority or DEFAULT_EVENT_PRIORITY)

    def remove_event_listener(self, event_type: Union[EventType, ChainEventType], handler: Callable):
        """
        移除事件处理器，将处理器从对应事件的订阅列表中删除
        :param event_type: 事件类型 (EventType 或 ChainEventType)
        :param handler: 要移除的处理器
        """
        self.__registry.remove(event_type, handler)

    def disable_event_handler(self, target: Union[Callable, type],
                              instance_key: Optional[str] = None):
        """
        禁用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        :param instance_key: 运行实例的实例键，给出时只停用该实例，兄弟实例继续响应
        """
        self.__registry.disable(target, instance_key)

    def enable_event_handler(self, target: Union[Callable, type],
                             instance_key: Optional[str] = None):
        """
        启用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        :param instance_key: 运行实例的实例键，给出时只启用该实例，不改变整类的停用状态
        """
        self.__registry.enable(target, instance_key)

    def visualize_handlers(self) -> List[Dict]:
        """
        可视化所有事件处理器，包括是否被禁用的状态
        :return: 处理器列表，包含事件类型、处理器标识符、优先级（如果有）和状态
        """

        return self.__registry.visualize()

    @classmethod
    def __get_handler_identifier(cls, target: Union[Callable, type]) -> Optional[str]:
        """
        获取处理器或处理器类的唯一标识符，包括模块名和类名/方法名
        :param target: 处理器函数或类
        :return: 唯一标识符
        """
        return EventRegistry.handler_identifier(target)

    @classmethod
    def __get_class_from_callable(cls, handler: Callable) -> Optional[str]:
        """
        获取可调用对象所属类的唯一标识符
        :param handler: 可调用对象（函数、方法等）
        :return: 类的唯一标识符
        """
        return EventRegistry.handler_class_identifier(handler)

    def __is_handler_enabled(self, handler: Callable) -> bool:
        """
        检查处理器是否已启用（没有被禁用）
        :param handler: 处理器函数
        :return: 如果处理器启用则返回 True，否则返回 False
        """
        return self.__registry.is_handler_enabled(handler)

    def __trigger_chain_event(self, event: Event) -> Optional[Event]:
        """
        触发链式事件，按顺序调用订阅的处理器，并记录处理耗时
        """
        logger.debug(f"Triggering synchronous chain event: {event}")
        dispatch = self.__dispatch_chain_event(event)
        return event if dispatch else None

    async def __trigger_chain_event_async(self, event: Event) -> Optional[Event]:
        """
        异步触发链式事件，按顺序调用订阅的处理器，并记录处理耗时
        """
        logger.debug(f"Triggering asynchronous chain event: {event}")
        dispatch = await self.__dispatch_chain_event_async(event)
        return event if dispatch else None

    def __trigger_broadcast_event(self, event: Event):
        """
        触发广播事件，将事件插入到优先级队列中
        :param event: 要处理的事件对象
        """
        logger.debug(f"Triggering broadcast event: {event}")
        self.__event_queue.put((event.priority, event))
        record_metric(
            "event.queue.depth",
            self.__event_queue.qsize(),
            delivery="broadcast",
        )

    def __dispatch_chain_event(self, event: Event) -> bool:
        """
        同步方式调度链式事件，按优先级顺序逐个调用事件处理器，并记录每个处理器的处理时间
        :param event: 要调度的事件对象
        """
        return self.__dispatcher.dispatch_chain(event)

    async def __dispatch_chain_event_async(self, event: Event) -> bool:
        """
        异步方式调度链式事件，按优先级顺序逐个调用事件处理器，并记录每个处理器的处理时间
        :param event: 要调度的事件对象
        """
        return await self.__dispatcher.async_dispatch_chain(event)

    def __dispatch_broadcast_event(self, event: Event):
        """
        异步方式调度广播事件，通过线程池逐个调用事件处理器
        :param event: 要调度的事件对象
        """
        self.__dispatcher.dispatch_broadcast(event)

    @classmethod
    def __should_dispatch_to_target_plugin(
            cls,
            handler: Callable,
            handler_identifier: str,
            target_plugin_id: str,
    ) -> bool:
        """
        限定插件输入事件只投递给目标插件，避免自由文本被其他插件观察到。
        """
        return EventDispatcher.should_dispatch_to_target_plugin(
            handler,
            handler_identifier,
            target_plugin_id,
        )

    def __safe_invoke_handler(self, handler: Callable, event: Event):
        """
        调用处理器，处理链式或广播事件
        :param handler: 处理器
        :param event: 事件对象
        """
        self.__dispatcher.safe_invoke_sync(handler, event)

    async def __safe_invoke_handler_async(self, handler: Callable, event: Event):
        """
        异步调用处理器，处理链式事件
        :param handler: 处理器
        :param event: 事件对象
        """
        await self.__dispatcher.safe_invoke_async(handler, event)

    def __invoke_handler_by_type_sync(self, handler: Callable, event: Event):
        """
        同步方式根据处理器类型调用相应的方法
        :param handler: 处理器
        :param event: 要处理的事件对象
        """
        self.__dispatcher.invoke_sync(handler, event)

    async def __invoke_handler_by_type_async(self, handler: Callable, event: Event):
        """
        异步方式根据处理器类型调用相应的方法
        :param handler: 处理器
        :param event: 要处理的事件对象
        """
        await self.__dispatcher.invoke_async(handler, event)

    @staticmethod
    def __parse_handler_names(handler: Callable) -> Tuple[str, str]:
        """
        解析处理器的类名和方法名
        :param handler: 处理器
        :return: (class_name, method_name)
        """
        return EventBindingResolver.parse_handler_names(handler)

    @staticmethod
    def __get_handler_owner_class(handler: Callable) -> Optional[Type[Any]]:
        """从处理器对象本身解析声明它的类，不按命名约定动态导入模块。"""
        return EventBindingResolver.owner_class(handler)

    def __resolve_handler(
            self,
            handler: Callable,
    ) -> List[Tuple[Callable, EventHandlerBinding, str, str]]:
        """将装饰阶段保存的函数解析为当前运行实例上的可调用方法列表。"""
        return self.__binding_resolver.resolve(handler)

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
                record_metric(
                    "event.queue.depth",
                    self.__event_queue.qsize(),
                    delivery="broadcast",
                )
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

    def __handle_event_error(self, event: Event, module_name: str,
                             class_name: str, method_name: str, e: Exception):
        """
        全局错误处理器，用于处理事件处理中的异常
        """
        self.__error_policy.handle(
            event=event,
            module_name=module_name,
            class_name=class_name,
            method_name=method_name,
            error=e,
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


# 模块热重载时类对象会重新创建，但插件和 SDK 可能仍持有旧全局实例。把旧实例登记到
# 新 EventManager 类的单例键，确保所有公开入口继续共享同一个事件总线。
_existing_eventmanager = globals().get("eventmanager")
if _existing_eventmanager is not None:
    Singleton._instances[(EventManager, (), frozenset())] = _existing_eventmanager
eventmanager = EventManager()
