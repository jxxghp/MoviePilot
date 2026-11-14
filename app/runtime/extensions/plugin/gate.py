"""
插件生命周期的互斥执行权和慢钩子诊断，仅在更新所有权时短暂持锁。
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginLifecycleActivity


@dataclass(frozen=True, slots=True)
class _Operation:
    """
    同一执行线程内嵌套的生命周期操作或钩子。

    :param operation: 操作名称
    :param plugin_id: 当前处理的插件 ID；全局准备阶段为空
    :param hook: 当前钩子或加载阶段；未进入时为空
    :param started_at: 单调时钟记录的进入时刻，单位为秒
    """

    operation: str
    plugin_id: Optional[str]
    hook: Optional[str]
    started_at: float


class PluginLifecycleGate:
    """
    保留共享注册表的串行约束，竞争者立即拒绝，不持锁运行第三方代码。
    """

    def __init__(self) -> None:
        """
        初始化执行线程归属与嵌套操作栈。
        """
        self._lock = threading.RLock()
        self._thread_id: Optional[int] = None
        self._operations: list[_Operation] = []

    @contextmanager
    def hold(
        self, operation: str, plugin_id: Optional[str] = None, hook: Optional[str] = None,
    ) -> Iterator[None]:
        """
        为当前线程取得执行权，允许同步停启嵌套，其他线程快速失败。

        :raises PluginMutationRejectedError: 另一个线程仍持有真实执行权
        """
        current_thread = threading.get_ident()
        with self._lock:
            if self._operations and self._thread_id != current_thread:
                activity = self.activity()
                assert activity is not None
                raise PluginMutationRejectedError(
                    operation,
                    f"插件生命周期正忙：{activity.plugin_id or '全部插件'} "
                    f"{activity.operation}/{activity.hook or '准备'} "
                    f"已执行 {activity.elapsed_seconds:.2f} 秒",
                )
            self._thread_id = current_thread
            self._operations.append(_Operation(operation, plugin_id, hook, time.monotonic()))
        try:
            yield
        finally:
            with self._lock:
                self._operations.pop()
                if not self._operations:
                    self._thread_id = None

    def activity(self) -> Optional[PluginLifecycleActivity]:
        """
        返回当前真实执行者及钩子耗时，不等待其第三方代码完成。
        """
        with self._lock:
            if not self._operations:
                return None
            root, current = self._operations[0], self._operations[-1]
            now = time.monotonic()
            return PluginLifecycleActivity(
                operation=root.operation, plugin_id=current.plugin_id,
                hook=current.hook, elapsed_seconds=now - root.started_at,
                hook_elapsed_seconds=now - current.started_at if current.hook else 0,
            )
