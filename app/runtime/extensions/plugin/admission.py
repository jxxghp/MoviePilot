"""
插件宿主可变事务的停机准入。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from app.schemas.exception import PluginMutationRejectedError
from app.schemas.plugin import PluginLifecycleActivity


@dataclass(slots=True)
class _MutationContext:
    """
    记录一个可跨协程和受控线程传播的事务上下文。

    :param admission: 负责本次变更事务准入的控制器
    :param operation: 根事务的操作说明
    :param started_at: 根事务开始的单调时钟秒数
    :param holders: 持有该上下文的调用数，初始为 0
    :param open: 上下文是否仍接受后续持有者，初始为 True
    """

    admission: "PluginMutationAdmission"
    operation: str
    started_at: float
    holders: int = 0
    open: bool = True


class PluginMutationAdmission:
    """
    在停机封口与插件可变事务之间维护真实 owner 计数。
    """

    def __init__(
        self, activity: Optional[Callable[[], Optional[PluginLifecycleActivity]]] = None,
    ) -> None:
        """
        初始化开放准入、活动 owner 计数和事务上下文。
        """
        self._condition = threading.Condition()
        self._accepting = True
        self._active_count = 0
        self._owner: Optional[_MutationContext] = None
        self._lifecycle_activity = activity or (lambda: None)
        self._current_context: ContextVar[_MutationContext | None] = ContextVar(
            "plugin_mutation_context",
            default=None,
        )

    @property
    def active_count(self) -> int:
        """
        返回尚未退出的 lease 数量，用于停机诊断和严格卸载判断。
        """
        with self._condition:
            return self._active_count

    @property
    def accepting(self) -> bool:
        """
        返回当前生命周期是否仍接纳新的根事务。
        """
        with self._condition:
            return self._accepting

    def is_held(self) -> bool:
        """
        判断当前执行上下文是否持有仍有效的本 admission lease。
        """
        context = self._current_context.get()
        with self._condition:
            return bool(
                context
                and context.admission is self
                and context.open
                and context.holders > 0
            )

    @contextmanager
    def hold(self, operation: str) -> Iterator[None]:
        """
        取得完整变更事务的执行权；竞争者快速拒绝，嵌套调用沿用真实 owner。

        共享源码导入、注册表及补偿流程需要串行执行。执行权在副作用前取得，
        避免保存配置或替换源码之后才因生命周期繁忙失败；不在事件循环中等同步锁。
        """
        context = self._current_context.get()
        context_token = None
        acquired = False
        try:
            with self._condition:
                nested = bool(
                    context
                    and context.admission is self
                    and context.open
                    and context.holders > 0
                )
                if not self._accepting and not nested:
                    raise PluginMutationRejectedError(operation)
                if not nested:
                    if self._owner is not None:
                        activity = self.activity()
                        assert activity is not None
                        raise PluginMutationRejectedError(
                            operation,
                            f"插件变更正忙：{activity.plugin_id or activity.operation} "
                            f"{activity.hook or activity.operation} "
                            f"已执行 {activity.elapsed_seconds:.2f} 秒",
                        )
                    context = _MutationContext(self, operation, time.monotonic())
                    self._owner = context
                    context_token = self._current_context.set(context)
                assert context is not None
                self._active_count += 1
                context.holders += 1
                acquired = True
            yield
        finally:
            if acquired:
                # acquired 只会在上下文已验证且 owner 计数增加后置为 True。
                assert context is not None
                with self._condition:
                    self._active_count -= 1
                    context.holders -= 1
                    if context.holders == 0:
                        context.open = False
                        if self._owner is context:
                            self._owner = None
                    self._condition.notify_all()
            if context_token is not None:
                self._current_context.reset(context_token)

    def seal(self) -> int:
        """
        原子停止接纳根事务，并返回封口瞬间的活动 lease 数量。
        """
        with self._condition:
            self._accepting = False
            return self._active_count

    def activity(self) -> Optional[PluginLifecycleActivity]:
        """
        返回真实根事务或其正在执行的钩子，调用方超时不会清除此记录。
        """
        with self._condition:
            if self._owner is None:
                return None
            return self._lifecycle_activity() or PluginLifecycleActivity(
                operation=self._owner.operation,
                elapsed_seconds=time.monotonic() - self._owner.started_at,
            )

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """
        按预算等待已获准 lease 退出；超时不取消或释放真实 owner。

        :param timeout: 最长等待秒数；为空时保留兼容的自然等待语义
        :return: 所有 lease 均已退出时为 True，预算耗尽时为 False
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active_count:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def reopen(self) -> bool:
        """
        仅在没有遗留 lease 时为新的应用生命周期重新开放准入。
        """
        with self._condition:
            if self._active_count:
                return False
            self._accepting = True
            return True
