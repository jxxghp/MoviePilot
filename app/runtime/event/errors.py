"""事件处理异常的通知、降级和递归保护策略。"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any, Optional

from app.runtime.log import logger
from app.schemas.types import EventType


EventErrorNotifier = Callable[[str, str], object]


class EventErrorPolicy:
    """隔离处理器异常，并阻止 SystemError 处理失败再次广播。"""

    def __init__(
        self,
        *,
        notifier: Callable[[], Optional[EventErrorNotifier]],
        emit_system_error: Callable[[dict[str, Any]], object],
    ) -> None:
        """注入通知读取器和 SystemError 发送回调。"""
        self._notifier = notifier
        self._emit_system_error = emit_system_error

    def handle(
        self,
        *,
        event: Any,
        module_name: str,
        class_name: str,
        method_name: str,
        error: Exception,
    ) -> None:
        """记录并通知异常；SystemError 自身失败时只降级写日志。"""
        trace = traceback.format_exc()
        logger.error("%s 事件处理出错：%s - %s", module_name, str(error), trace)
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
