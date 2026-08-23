"""插件宿主可变事务的停机准入。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.schemas.exception import PluginMutationRejectedError


@dataclass(slots=True)
class _MutationContext:
    """记录一个可跨协程和受控线程传播的事务上下文。"""

    admission: "PluginMutationAdmission"
    holders: int = 0
    open: bool = True


class PluginMutationAdmission:
    """在停机封口与插件可变事务之间维护真实 owner 计数。"""

    def __init__(self) -> None:
        """初始化开放准入、活动 owner 计数和事务上下文。"""
        self._condition = threading.Condition()
        self._accepting = True
        self._active_count = 0
        self._current_context: ContextVar[_MutationContext | None] = ContextVar(
            "plugin_mutation_context",
            default=None,
        )

    @property
    def active_count(self) -> int:
        """返回尚未退出的 lease 数量，用于停机诊断和严格卸载判断。"""
        with self._condition:
            return self._active_count

    @property
    def accepting(self) -> bool:
        """返回当前生命周期是否仍接纳新的根事务。"""
        with self._condition:
            return self._accepting

    def is_held(self) -> bool:
        """判断当前执行上下文是否持有仍有效的本 admission lease。"""
        context = self._current_context.get()
        return bool(
            context
            and context.admission is self
            and context.open
            and context.holders > 0
        )

    @contextmanager
    def hold(self, operation: str) -> Iterator[None]:
        """取得可变事务 lease；封口后仅允许已获准事务的嵌套调用。"""
        context = self._current_context.get()
        nested = bool(
            context
            and context.admission is self
            and context.open
            and context.holders > 0
        )
        context_token = None
        if not nested:
            context = _MutationContext(admission=self)
            context_token = self._current_context.set(context)
        assert context is not None

        acquired = False
        try:
            with self._condition:
                if not self._accepting and not nested:
                    raise PluginMutationRejectedError(operation)
                self._active_count += 1
                context.holders += 1
                acquired = True
            yield
        finally:
            if acquired:
                with self._condition:
                    self._active_count -= 1
                    context.holders -= 1
                    if context.holders == 0:
                        context.open = False
                    self._condition.notify_all()
            if context_token is not None:
                self._current_context.reset(context_token)

    def seal(self) -> int:
        """原子停止接纳根事务，并返回封口瞬间的活动 lease 数量。"""
        with self._condition:
            self._accepting = False
            return self._active_count

    def wait_until_idle(self) -> None:
        """自然等待全部已获准 lease 退出，不取消或遗失其 owner。"""
        with self._condition:
            while self._active_count:
                self._condition.wait()

    def reopen(self) -> bool:
        """仅在没有遗留 lease 时为新的应用生命周期重新开放准入。"""
        with self._condition:
            if self._active_count:
                return False
            self._accepting = True
            return True
