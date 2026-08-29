import asyncio
import concurrent.futures
import random
import threading
import traceback
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from queue import Empty, PriorityQueue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar, Union

from app.foundation.singleton import Singleton
from app.runtime.correlation import get_correlation_id
from app.runtime.event.binding import (
    EventBindingResolver,
    EventHandlerBinding,
    HandlerInstanceResolver,
)
from app.runtime.event.contracts import normalize_event_type, validate_event_payload
from app.runtime.event.dispatch import EventDispatcher
from app.runtime.event.errors import EventErrorNotifier, EventErrorPolicy
from app.runtime.event.registry import EventRegistry
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.observability import record_metric
from app.runtime.rate import ExponentialBackoffRateLimiter
from app.runtime.thread import ThreadHelper
from app.schemas.event import ChainEventData
from app.schemas.types import ChainEventType, EventType

if TYPE_CHECKING:
    from app.runtime.event.snapshot import EventPayloadSnapshot

DEFAULT_EVENT_PRIORITY = 10  # 事件的默认优先级
MIN_EVENT_CONSUMER_THREADS = 1  # 最小事件消费者线程数
INITIAL_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 1  # 事件队列空闲时的初始超时时间（秒）
MAX_EVENT_QUEUE_IDLE_TIMEOUT_SECONDS = 5  # 事件队列空闲时的最大超时时间（秒）
_EVENT_STOP_SENTINEL = object()
_CURRENT_EVENT_HANDLER_OWNER: ContextVar[object | None] = ContextVar(
    "current_event_handler_owner",
    default=None,
)
_EventHandler = TypeVar("_EventHandler", bound=Callable[..., Any])
_EventKind = Union[EventType, ChainEventType]
_EventRegistration = Union[
    _EventKind,
    List[_EventKind],
    Type[EventType],
    Type[ChainEventType],
]


@dataclass(slots=True)
class _EventAsyncHandle:
    """记录异步广播的取消代理和真实完成信号。"""

    handle: concurrent.futures.Future[Any]
    completion: concurrent.futures.Future[Any]


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

    def snapshot(self) -> "EventPayloadSnapshot":
        """返回插件可安全读取的类型化 payload 快照，原始 event_data 保持不变。"""
        from app.runtime.event.snapshot import snapshot_event_data

        return snapshot_event_data(self.event_type, self.event_data)

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
        # 线程锁
        self.__lock = threading.Lock()
        # 退出事件
        self.__event = threading.Event()
        # 广播处理器由事件总线统一持有，确保插件卸载前可以建立结算屏障。
        self.__lifecycle_lock = threading.RLock()
        self.__lifecycle_state = "new"
        self.__sync_handles: Dict[object, concurrent.futures.Future[Any]] = {}
        self.__async_handles: Dict[object, _EventAsyncHandle] = {}
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
        )
        self.__binding_resolver = EventBindingResolver(
            lock=self.__lock,
            resolvers=lambda: self.__handler_instance_resolvers,
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
            event_factory=Event,
            error_handler=lambda **kwargs: self.__handle_event_error(**kwargs),
            async_handle_sink=self.__register_async_handle,
            sync_handle_sink=self.__register_sync_handle,
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

    def unregister_handler_instance_resolver(self, name: str) -> None:
        """撤销命名实例解析器；重复撤销不影响其它 owner。"""
        self.__binding_resolver.unregister(name)

    def unresolved_handler_bindings(self) -> tuple[str, ...]:
        """返回未命中显式 resolver 的类处理器诊断清单。"""
        return self.__binding_resolver.unresolved_handlers()

    def set_error_notifier(self, notifier: Optional[EventErrorNotifier]) -> None:
        """设置事件处理异常的外部通知回调。"""
        with self.__lock:
            self.__error_notifier = notifier

    def reset_error_notifier(self) -> None:
        """撤销当前 lifespan 的事件错误通知回调。"""
        self.set_error_notifier(None)

    def start(self):
        """
        开始广播事件处理线程
        """
        with self.__lifecycle_lock:
            if self.__lifecycle_state == "running":
                return
            if self.__lifecycle_state == "stopping":
                logger.warning("事件处理仍在停止，忽略重复启动")
                return
            if self.__lifecycle_state == "sealed":
                logger.warning("事件处理已封口，忽略重复启动")
                return
            self.__lifecycle_state = "running"
            self.__event.set()
            self.__consumer_threads = []
        # 启动消费者线程用于处理广播事件
        for _ in range(MIN_EVENT_CONSUMER_THREADS):
            thread = threading.Thread(target=self.__broadcast_consumer_loop, daemon=True)
            thread.start()
            self.__consumer_threads.append(thread)  # 将线程对象保存到列表中

    def stop(self):
        """
        兼容同步关闭入口，等待同步处理器并请求取消异步处理器。

        调用线程无法安全等待主事件循环完成异步清理；需要完整异步收口时应使用
        stop_async()。
        """
        logger.info("正在停止事件处理...")
        consumer_threads = self.__begin_stop()
        try:
            self.__join_consumer_threads(consumer_threads)
            self.__discard_stop_sentinels()
            current_owner = _CURRENT_EVENT_HANDLER_OWNER.get()
            self.__cancel_async_handles(exclude_owner=current_owner)
            self.__wait_sync_handles(exclude_owner=current_owner)
            logger.info("事件处理停止完成")
        except Exception as e:
            logger.error(f"停止事件处理线程出错：{str(e)} - {traceback.format_exc()}")
        finally:
            with self.__lifecycle_lock:
                self.__lifecycle_state = "stopped"
                self.__consumer_threads = []

    async def stop_async(self) -> None:
        """停止广播消费者，等待同步处理器并取消收口异步处理器。"""
        logger.info("正在停止事件处理...")
        consumer_threads = self.__begin_stop()
        try:
            if consumer_threads:
                await asyncio.to_thread(
                    self.__join_consumer_threads,
                    consumer_threads,
                )
            self.__discard_stop_sentinels()
            current_owner = _CURRENT_EVENT_HANDLER_OWNER.get()
            with self.__lifecycle_lock:
                async_handles = tuple(
                    handle
                    for owner, handle in self.__async_handles.items()
                    if owner is not current_owner
                )
                sync_handles = tuple(
                    handle
                    for owner, handle in self.__sync_handles.items()
                    if owner is not current_owner
                )
            for handle in async_handles:
                handle.handle.cancel()
            if async_handles or sync_handles:
                await asyncio.gather(
                    *(
                        asyncio.shield(asyncio.wrap_future(handle.completion))
                        for handle in async_handles
                    ),
                    *(
                        asyncio.shield(asyncio.wrap_future(handle))
                        for handle in sync_handles
                    ),
                    return_exceptions=True,
                )
            logger.info("事件处理停止完成")
        except Exception as e:
            logger.error(f"停止事件处理线程出错：{str(e)} - {traceback.format_exc()}")
            raise
        with self.__lifecycle_lock:
            self.__lifecycle_state = "stopped"
            self.__consumer_threads = []

    def __begin_stop(self) -> tuple[threading.Thread, ...]:
        """关闭提交入口并唤醒消费者线程。"""
        with self.__lifecycle_lock:
            self.__lifecycle_state = "stopping"
            self.__event.clear()
            consumer_threads = tuple(self.__consumer_threads)
            if consumer_threads:
                self.__event_queue.put((float("-inf"), _EVENT_STOP_SENTINEL))
        return consumer_threads

    @staticmethod
    def __join_consumer_threads(consumer_threads: tuple[threading.Thread, ...]) -> None:
        """在线程池或同步兼容入口中等待事件消费者退出。"""
        for consumer_thread in consumer_threads:
            consumer_thread.join()

    def __discard_stop_sentinels(self) -> None:
        """清理仅用于唤醒消费者的标记，保留尚未消费的业务事件。"""
        pending = []
        while True:
            try:
                item = self.__event_queue.get_nowait()
            except Empty:
                break
            if item[1] is not _EVENT_STOP_SENTINEL:
                pending.append(item)
            self.__event_queue.task_done()
        for item in pending:
            self.__event_queue.put(item)

    def __cancel_async_handles(
            self,
            *,
            exclude_owner: object | None = None,
    ) -> None:
        """请求取消异步处理器，并避免 handler 关闭事件总线时取消自身。"""
        with self.__lifecycle_lock:
            handles = tuple(
                handle
                for owner, handle in self.__async_handles.items()
                if owner is not exclude_owner
            )
        for handle in handles:
            handle.handle.cancel()

    def __wait_sync_handles(
            self,
            *,
            exclude_owner: object | None = None,
    ) -> None:
        """等待同步处理器完成，并避免 handler 关闭事件总线时等待自身。"""
        with self.__lifecycle_lock:
            handles = tuple(
                handle
                for owner, handle in self.__sync_handles.items()
                if owner is not exclude_owner
            )
        if handles:
            concurrent.futures.wait(handles)

    def __register_sync_handle(
            self,
            callback: Callable[..., Any],
            args: tuple[Any, ...],
    ) -> bool:
        """在同一生命周期临界区提交并登记同步广播处理器。"""
        with self.__lifecycle_lock:
            if self.__lifecycle_state != "running":
                logger.warning(
                    "事件处理处于 %s 状态，拒绝同步广播处理器",
                    self.__lifecycle_state,
                )
                return False
            try:
                owner = object()
                completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
                self.__sync_handles[owner] = completion

                def _tracked_sync() -> Any:
                    """在同步 handler 调用栈中发布当前事件 owner。"""
                    context_token = _CURRENT_EVENT_HANDLER_OWNER.set(owner)
                    try:
                        return callback(*args)
                    finally:
                        _CURRENT_EVENT_HANDLER_OWNER.reset(context_token)

                handle = self.__executor.submit(_tracked_sync)
            except RuntimeError:
                self.__sync_handles.pop(owner, None)
                logger.warning("同步事件处理器无法投递，线程池已停止")
                return False

            def _complete_sync_handle(
                    completed: concurrent.futures.Future[Any],
            ) -> None:
                """把真实线程句柄的结果转移到已登记的结算句柄。"""
                if completed.cancelled():
                    completion.cancel()
                else:
                    error = completed.exception()
                    if error is not None:
                        completion.set_exception(error)
                    else:
                        completion.set_result(completed.result())
                self.__remove_sync_handle(owner)

            handle.add_done_callback(_complete_sync_handle)
        return True

    def __remove_sync_handle(self, owner: object) -> None:
        """同步处理器完成后移除其 owner 句柄。"""
        with self.__lifecycle_lock:
            self.__sync_handles.pop(owner, None)

    def __register_async_handle(
            self,
            coroutine: Any,
    ) -> bool:
        """在同一生命周期临界区提交并登记异步广播处理器。"""
        with self.__lifecycle_lock:
            if self.__lifecycle_state != "running":
                coroutine.close()
                logger.warning(
                    "事件处理处于 %s 状态，拒绝异步广播处理器",
                    self.__lifecycle_state,
                )
                return False
            completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
            started = threading.Event()
            owner = object()

            async def _tracked() -> None:
                started.set()
                context_token = _CURRENT_EVENT_HANDLER_OWNER.set(owner)
                try:
                    result = await coroutine
                except asyncio.CancelledError:
                    if not completion.done():
                        completion.cancel()
                except Exception as err:
                    if not completion.done():
                        completion.set_exception(err)
                else:
                    if not completion.done():
                        completion.set_result(result)
                finally:
                    _CURRENT_EVENT_HANDLER_OWNER.reset(context_token)

            tracked = _tracked()
            try:
                handle = asyncio.run_coroutine_threadsafe(
                    tracked,
                    main_loop_registry.require(),
                )
            except RuntimeError:
                tracked.close()
                coroutine.close()
                logger.warning("异步事件处理器无法投递，事件循环已停止")
                return False
            self.__async_handles[owner] = _EventAsyncHandle(
                handle=handle,
                completion=completion,
            )

            def _complete_unstarted_submission(
                    submitted: concurrent.futures.Future[Any],
            ) -> None:
                if submitted.cancelled() and not started.is_set():
                    coroutine.close()
                    completion.cancel()

            handle.add_done_callback(_complete_unstarted_submission)
        completion.add_done_callback(
            lambda _completed, current_owner=owner: (
                self.__remove_async_handle(current_owner)
            )
        )
        return True

    def __remove_async_handle(self, owner: object) -> None:
        """异步处理器完成后移除其 owner 句柄。"""
        with self.__lifecycle_lock:
            self.__async_handles.pop(owner, None)

    async def drain_async(
            self,
            timeout: Optional[float] = None,
            *,
            seal: bool = False,
    ) -> bool:
        """
        等待已接纳的广播事件及其派生事件自然结算。

        seal=True 会在确认稳定空闲的同一临界区关闭后续广播提交，供插件卸载前
        建立不可穿透的投递屏障；超时或停止交错时保持原提交状态并返回 False。
        广播处理器调用栈内无法等待自身完成，因此会立即返回 False，且不执行封口。
        """
        if _CURRENT_EVENT_HANDLER_OWNER.get() is not None:
            logger.warning("事件处理器内部不能建立事件投递屏障")
            return False
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(timeout, 0)
        while True:
            with self.__lifecycle_lock:
                if self.__lifecycle_state not in {"running", "sealed"}:
                    return False
                with self.__event_queue.all_tasks_done:
                    queue_idle = self.__event_queue.unfinished_tasks == 0
                handlers_idle = not self.__sync_handles and not self.__async_handles
                if queue_idle and handlers_idle:
                    if seal:
                        self.__lifecycle_state = "sealed"
                    return True
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.01, remaining))
            else:
                await asyncio.sleep(0.01)

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

    def send_event_strict(
        self,
        etype: EventType,
        data: Optional[Union[dict[str, object], ChainEventData]] = None,
        priority: Optional[int] = DEFAULT_EVENT_PRIORITY,
    ) -> Event:
        """同步等待全部广播处理器完成，任一失败时阻止 durable 消息结算。"""
        event = Event(etype, data, priority)
        with self.__lifecycle_lock:
            if self.__lifecycle_state != "running":
                raise RuntimeError(f"事件处理处于 {self.__lifecycle_state} 状态")
        self.__dispatcher.dispatch_broadcast_strict(
            event,
            self.__wait_strict_async_handler,
        )
        return event

    @staticmethod
    def __wait_strict_async_handler(coroutine: Any) -> Any:
        """在主事件循环等待异步处理器，禁止循环线程同步等待自身。"""
        loop = main_loop_registry.require()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            coroutine.close()
            raise RuntimeError("主事件循环线程不能同步等待 durable 事件处理器")
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

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

    def disable_event_handler(self, target: Union[Callable, type]):
        """
        禁用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        """
        self.__registry.disable(target)

    def enable_event_handler(self, target: Union[Callable, type]):
        """
        启用指定的事件处理器或事件处理器类
        :param target: 处理器函数或类
        """
        self.__registry.enable(target)

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
        with self.__lifecycle_lock:
            if self.__lifecycle_state in {"sealed", "stopping", "stopped"}:
                logger.warning(
                    "事件处理处于 %s 状态，拒绝广播事件 %s",
                    self.__lifecycle_state,
                    event.event_type,
                )
                return None
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
    ) -> Optional[Tuple[Callable, EventHandlerBinding, str, str]]:
        """将装饰阶段保存的函数解析为当前运行实例上的可调用方法。"""
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
                try:
                    if event is _EVENT_STOP_SENTINEL:
                        break
                    record_metric(
                        "event.queue.depth",
                        self.__event_queue.qsize(),
                        delivery="broadcast",
                    )
                    rate_limiter.reset()
                    self.__dispatch_broadcast_event(event)
                finally:
                    self.__event_queue.task_done()
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

    def register(
            self,
            etype: _EventRegistration,
            priority: Optional[int] = DEFAULT_EVENT_PRIORITY,
    ) -> Callable[[_EventHandler], _EventHandler]:
        """
        事件注册装饰器，用于将函数注册为事件的处理器
        :param etype:
            - 单个事件类型成员 (如 EventType.MetadataScrape, ChainEventType.PluginAction)
            - 事件类型类 (EventType, ChainEventType)
            - 或事件类型成员的列表
        :param priority: 可选，链式事件的优先级，默认为 DEFAULT_EVENT_PRIORITY
        """

        def decorator(f: _EventHandler) -> _EventHandler:
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
                elif event is EventType or event is ChainEventType:
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
