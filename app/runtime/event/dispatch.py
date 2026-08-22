"""链式和广播事件的独立调度算法。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from app.runtime.event.binding import EventBindingResolver, EventHandlerBinding
from app.runtime.event.registry import EventRegistry
from app.runtime.execution import run_in_threadpool
from app.runtime.extensions.contract.instance import split_instance_key
from app.runtime.log import logger, wrap_for_plugin_instance
from app.runtime.correlation import correlation_scope
from app.runtime.observability import observe_duration
from app.schemas.types import EventType


def _bind_instance_context(method: Callable, binding: EventHandlerBinding) -> Callable:
    """
    按处理器绑定的实例键把方法包装到对应插件实例的日志上下文中。

    `instance_key` 由事件总线的实例解析器（目前只有插件管理器登记了这个 resolver）
    提供，因此非空时可放心拆成 (插件标识, 实例标识)；未登记 resolver 的处理器
    没有 instance_key，原样返回，按插件级栈回溯路由，不归入具体实例。
    :param method: 待调用的处理器绑定方法
    :param binding: 该方法的实例绑定信息
    :return: 包装后（或原样）的可调用对象
    """
    if not binding.instance_key:
        return method
    plugin_id, instance_id = split_instance_key(binding.instance_key)
    return wrap_for_plugin_instance(method, plugin_id, instance_id)


class EventDispatcher:
    """基于订阅快照执行链式或广播事件，不拥有注册和生命周期状态。"""

    def __init__(
        self,
        *,
        registry: EventRegistry,
        binding_resolver: EventBindingResolver,
        executor: Callable[[], Any],
        event_loop: Callable[[], Any],
        event_factory: Callable[..., Any],
        error_handler: Callable[..., None],
    ) -> None:
        """注入注册表、绑定器、执行器和错误策略回调。"""
        self._registry = registry
        self._binding_resolver = binding_resolver
        self._executor = executor
        self._event_loop = event_loop
        self._event_factory = event_factory
        self._error_handler = error_handler

    def dispatch_chain(self, event: Any) -> bool:
        """同步按优先级顺序执行链式事件快照。"""
        handlers = self._registry.chain_snapshot(event.event_type)
        enabled = tuple(
            (handler_id, priority, handler)
            for handler_id, (priority, handler) in handlers
            if self._registry.is_handler_enabled(handler)
        )
        if not enabled:
            logger.debug("No enabled handlers found for chain event: %s", event)
            return False
        self._log_lifecycle(event, "Started")
        for _handler_id, priority, handler in enabled:
            started_at = time.time()
            self.invoke_sync(handler, event)
            logger.debug(
                "%s (Priority: %s), completed in %.3fs for event: %s",
                EventRegistry.handler_identifier(handler),
                priority,
                time.time() - started_at,
                event,
            )
        self._log_lifecycle(event, "Completed")
        return True

    async def async_dispatch_chain(self, event: Any) -> bool:
        """异步按优先级顺序执行链式事件快照。"""
        handlers = self._registry.chain_snapshot(event.event_type)
        enabled = tuple(
            (handler_id, priority, handler)
            for handler_id, (priority, handler) in handlers
            if self._registry.is_handler_enabled(handler)
        )
        if not enabled:
            logger.debug("No enabled handlers found for chain event: %s", event)
            return False
        self._log_lifecycle(event, "Started")
        for _handler_id, priority, handler in enabled:
            started_at = time.time()
            await self.invoke_async(handler, event)
            logger.debug(
                "%s (Priority: %s), completed in %.3fs for event: %s",
                EventRegistry.handler_identifier(handler),
                priority,
                time.time() - started_at,
                event,
            )
        self._log_lifecycle(event, "Completed")
        return True

    def dispatch_broadcast(self, event: Any) -> None:
        """按订阅快照把广播事件投递到线程池或主事件循环。"""
        handlers = self._registry.broadcast_snapshot(event.event_type)
        if not handlers:
            logger.debug("No handlers found for broadcast event: %s", event)
            return
        target_plugin_id = None
        if event.event_type == EventType.MessageAction and isinstance(
            event.event_data,
            dict,
        ):
            target_plugin_id = event.event_data.get("__mp_target_plugin_id")
        for handler_id, handler in handlers:
            if target_plugin_id and not self.should_dispatch_to_target_plugin(
                handler,
                handler_id,
                str(target_plugin_id),
            ):
                continue
            if isinstance(event.event_data, dict):
                event_data = event.event_data.copy()
                event_data.pop("__mp_target_plugin_id", None)
            else:
                event_data = event.event_data
            isolated = self._event_factory(
                event_type=event.event_type,
                event_data=event_data,
                priority=event.priority,
                correlation_id=event.correlation_id,
            )
            if inspect.iscoroutinefunction(handler):
                asyncio.run_coroutine_threadsafe(
                    self.safe_invoke_async(handler, isolated),
                    self._event_loop(),
                )
            else:
                self._executor().submit(
                    self.safe_invoke_sync,
                    handler,
                    isolated,
                )

    def safe_invoke_sync(self, handler: Callable, event: Any) -> None:
        """仅在处理器启用时执行同步调用。"""
        if self._registry.is_handler_enabled(handler):
            self.invoke_sync(handler, event)

    async def safe_invoke_async(self, handler: Callable, event: Any) -> None:
        """仅在处理器启用时执行异步调用。"""
        if self._registry.is_handler_enabled(handler):
            await self.invoke_async(handler, event)

    def invoke_sync(self, handler: Callable, event: Any) -> None:
        """解析实例绑定并逐个同步调用处理器；声明类不可解析时整体跳过。"""
        resolved = self._binding_resolver.resolve(handler)
        for method, binding, class_name, method_name in (
            EventBindingResolver.as_binding_sequence(resolved)
        ):
            with correlation_scope(event.correlation_id):
                try:
                    with observe_duration(
                        "event.handler.duration",
                        event_type=event.event_type.value,
                        handler_type="bound" if class_name else "function",
                    ):
                        _bind_instance_context(method, binding)(event)
                except Exception as err:
                    self._error_handler(
                        event=event,
                        module_name=binding.owner_name,
                        class_name=class_name,
                        method_name=method_name,
                        e=err,
                    )

    async def invoke_async(self, handler: Callable, event: Any) -> None:
        """解析实例绑定，逐个按处理器类型选择协程、线程池或同步调用；声明类不可解析时整体跳过。"""
        resolved = self._binding_resolver.resolve(handler)
        for method, binding, class_name, method_name in (
            EventBindingResolver.as_binding_sequence(resolved)
        ):
            with correlation_scope(event.correlation_id):
                try:
                    with observe_duration(
                        "event.handler.duration",
                        event_type=event.event_type.value,
                        handler_type="bound" if class_name else "function",
                    ):
                        bound_method = _bind_instance_context(method, binding)
                        if inspect.iscoroutinefunction(bound_method):
                            await bound_method(event)
                        elif binding.run_sync_in_threadpool or not class_name:
                            await run_in_threadpool(bound_method, event)
                        else:
                            bound_method(event)
                except Exception as err:
                    self._error_handler(
                        event=event,
                        module_name=binding.owner_name,
                        class_name=class_name,
                        method_name=method_name,
                        e=err,
                    )

    @staticmethod
    def should_dispatch_to_target_plugin(
        handler: Callable,
        handler_identifier: str,
        target_plugin_id: str,
    ) -> bool:
        """只把定向输入事件投递给标识和声明均匹配的目标插件。"""
        class_name, method_name = EventBindingResolver.parse_handler_names(handler)
        if class_name != target_plugin_id:
            return False
        parts = (handler_identifier or "").split(".")
        return len(parts) >= 2 and parts[-2:] == [class_name, method_name]

    @staticmethod
    def _log_lifecycle(event: Any, stage: str) -> None:
        """记录事件调度的开始和完成阶段。"""
        logger.debug("%s - %s", stage, event)
