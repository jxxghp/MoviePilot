"""事件订阅、禁用状态和调度快照注册表。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from app.runtime.log import logger
from app.schemas.types import ChainEventType, EventType


class EventRegistry:
    """集中管理事件处理器注册、启停和不可变调度快照。"""

    def __init__(
        self,
        *,
        lock: Any,
        broadcast_subscribers: Callable[[], dict],
        chain_subscribers: Callable[[], dict],
        disabled_handlers: Callable[[], set],
        disabled_classes: Callable[[], set],
    ) -> None:
        """绑定由兼容门面持有的存储，便于热重载和旧测试替换快照。"""
        self._lock = lock
        self._broadcast_subscribers = broadcast_subscribers
        self._chain_subscribers = chain_subscribers
        self._disabled_handlers = disabled_handlers
        self._disabled_classes = disabled_classes

    @staticmethod
    def handler_identifier(target: Callable | type) -> str:
        """返回包含模块和限定名的稳定处理器标识。"""
        module = inspect.getmodule(target)
        module_name = module.__name__ if module else "unknown_module"
        return f"{module_name}.{target.__qualname__}"

    @classmethod
    def handler_class_identifier(cls, handler: Callable) -> str | None:
        """返回可调用对象所属类的稳定标识；自由函数返回空值。"""
        if inspect.ismethod(handler) and hasattr(handler, "__self__"):
            return cls.handler_identifier(handler.__self__.__class__)
        if not inspect.isfunction(handler) and hasattr(handler, "__call__"):
            return cls.handler_identifier(handler.__class__)
        qualname_parts = handler.__qualname__.split(".")
        if len(qualname_parts) <= 1:
            return None
        module = inspect.getmodule(handler)
        module_name = module.__name__ if module else "unknown_module"
        return f"{module_name}.{'.'.join(qualname_parts[:-1])}"

    def is_handler_enabled(self, handler: Callable) -> bool:
        """判断处理器及其所属类是否均处于启用状态。"""
        handler_id = self.handler_identifier(handler)
        class_id = self.handler_class_identifier(handler)
        return not (
            handler_id in self._disabled_handlers()
            or (
                class_id is not None
                and class_id in self._disabled_classes()
            )
        )

    def check(self, event_type: EventType | ChainEventType) -> bool:
        """检查指定事件是否存在启用的处理器。"""
        if isinstance(event_type, ChainEventType):
            handlers = self._chain_subscribers().get(event_type, {})
            return any(
                self.is_handler_enabled(handler)
                for _, handler in handlers.values()
            )
        handlers = self._broadcast_subscribers().get(event_type, {})
        return any(self.is_handler_enabled(handler) for handler in handlers.values())

    def add(
        self,
        event_type: EventType | ChainEventType,
        handler: Callable,
        priority: int,
    ) -> None:
        """注册处理器，并为链式事件按优先级维护稳定顺序。"""
        with self._lock:
            handler_id = self.handler_identifier(handler)
            if isinstance(event_type, ChainEventType):
                subscribers = self._chain_subscribers()
                handlers = subscribers.setdefault(event_type, {})
                existed = handler_id in handlers
                handlers.pop(handler_id, None)
                if not existed:
                    logger.debug(
                        "Subscribed to chain event: %s, Priority: %s - %s",
                        event_type.value,
                        priority,
                        handler_id,
                    )
                handlers[handler_id] = (priority, handler)
                subscribers[event_type] = dict(
                    sorted(handlers.items(), key=lambda item: item[1][0])
                )
                return
            subscribers = self._broadcast_subscribers()
            handlers = subscribers.setdefault(event_type, {})
            existed = handler_id in handlers
            handlers.pop(handler_id, None)
            if not existed:
                logger.debug(
                    "Subscribed to broadcast event: %s - %s",
                    event_type.value,
                    handler_id,
                )
            handlers[handler_id] = handler

    def remove(
        self,
        event_type: EventType | ChainEventType,
        handler: Callable,
    ) -> None:
        """从指定事件中移除处理器。"""
        with self._lock:
            handler_id = self.handler_identifier(handler)
            if isinstance(event_type, ChainEventType):
                self._chain_subscribers().get(event_type, {}).pop(
                    handler_id,
                    None,
                )
                logger.debug(
                    "Unsubscribed from chain event: %s - %s",
                    event_type.value,
                    handler_id,
                )
                return
            self._broadcast_subscribers().get(event_type, {}).pop(
                handler_id,
                None,
            )
            logger.debug(
                "Unsubscribed from broadcast event: %s - %s",
                event_type.value,
                handler_id,
            )

    def disable(self, target: Callable | type) -> None:
        """禁用单个处理器或整个处理器类。"""
        identifier = self.handler_identifier(target)
        if isinstance(target, type):
            self._disabled_classes().add(identifier)
            logger.debug("Disabled event handler class - %s", identifier)
        else:
            self._disabled_handlers().add(identifier)
            logger.debug("Disabled event handler - %s", identifier)

    def enable(self, target: Callable | type) -> None:
        """重新启用单个处理器或整个处理器类。"""
        identifier = self.handler_identifier(target)
        if isinstance(target, type):
            self._disabled_classes().discard(identifier)
            logger.debug("Enabled event handler class - %s", identifier)
        else:
            self._disabled_handlers().discard(identifier)
            logger.debug("Enabled event handler - %s", identifier)

    def chain_snapshot(self, event_type: ChainEventType) -> tuple:
        """返回当前链式订阅快照，运行期变更从下一次事件生效。"""
        with self._lock:
            return tuple(self._chain_subscribers().get(event_type, {}).items())

    def broadcast_snapshot(self, event_type: EventType) -> tuple:
        """返回当前广播订阅快照，运行期变更从下一次事件生效。"""
        with self._lock:
            return tuple(
                self._broadcast_subscribers().get(event_type, {}).items()
            )

    def visualize(self) -> list[dict]:
        """导出所有订阅处理器的事件、优先级和启停状态。"""
        result = []
        combined = {
            **self._broadcast_subscribers(),
            **self._chain_subscribers(),
        }
        for event_type, subscribers in combined.items():
            for handler_id, handler_data in subscribers.items():
                if isinstance(handler_data, tuple) and len(handler_data) == 2:
                    priority, handler = handler_data
                else:
                    priority, handler = None, handler_data
                item = {
                    "event_type": event_type.value,
                    "handler_identifier": handler_id,
                    "status": (
                        "enabled"
                        if self.is_handler_enabled(handler)
                        else "disabled"
                    ),
                }
                if priority is not None:
                    item["priority"] = priority
                result.append(item)
        return result
