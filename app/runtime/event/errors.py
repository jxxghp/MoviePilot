"""事件处理异常的通知、降级和递归保护策略。"""

from __future__ import annotations

import threading
import traceback
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Optional

from app.runtime.log import logger
from app.schemas.types import EventType


EventErrorNotifier = Callable[[str, str], object]
_MAX_REPORTED_ERRORS = 4096


class EventErrorPolicy:
    """隔离处理器异常，并阻止 SystemError 处理失败再次广播。"""

    def __init__(
        self,
        *,
        notifier: Callable[[], Optional[EventErrorNotifier]],
        emit_system_error: Callable[[dict[str, Any]], object],
    ) -> None:
        """注入通知与错误广播回调，并初始化本进程有界提示去重记录。"""
        self._notifier = notifier
        self._emit_system_error = emit_system_error
        self._reported_errors: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._reported_errors_lock = threading.Lock()

    def _is_repeated_error(self, event: Any, handler: str, error: Exception) -> bool:
        """按持久事件及处理器错误去重提示；普通事件保持原行为，缓存有界。"""
        payload = event.event_data
        event_key = payload.get("idempotency_key") if isinstance(payload, dict) else None
        if not isinstance(event_key, str) or not event_key:
            return False
        key = (event.event_type.value, event_key, handler, str(error))
        with self._reported_errors_lock:
            if key in self._reported_errors:
                self._reported_errors.move_to_end(key)
                return True
            self._reported_errors[key] = None
            if len(self._reported_errors) > _MAX_REPORTED_ERRORS:
                self._reported_errors.popitem(last=False)
        return False

    def handle(
        self,
        *,
        event: Any,
        module_name: str,
        class_name: str,
        method_name: str,
        error: Exception,
    ) -> None:
        """每次失败保留日志，仅对同一持久事件的相同错误提示一次。

        Outbox 仍按严格回执重试和结算；去重只作用于本进程的系统提示及错误广播，
        不吞掉原处理器异常，也不把未完成的持久事件误标为完成。
        """
        trace = traceback.format_exc()
        logger.error("%s 事件处理出错：%s - %s", module_name, str(error), trace)
        if self._is_repeated_error(
                event, f"{module_name}.{class_name}.{method_name}", error
        ):
            return
        notifier = self._notifier()
        if notifier:
            try:
                notifier(
                    f"{module_name} 处理事件 {event.event_type} 时出错",
                    f"{class_name}.{method_name}：{str(error)}",
                )
            except Exception as notify_error:
                logger.error("发送事件错误通知失败：%s", str(notify_error))
        if event.event_type == EventType.SystemError:
            logger.error(
                "SystemError 处理器再次失败，停止错误事件递归广播：%s.%s",
                class_name,
                method_name,
            )
            return
        self._emit_system_error(
            {
                "type": "event",
                "event_type": event.event_type,
                "event_handle": f"{class_name}.{method_name}",
                "error": str(error),
                "traceback": trace,
            }
        )
