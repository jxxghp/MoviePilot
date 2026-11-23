"""
进程内浏览器实例的公平准入与调用取消边界，不创建或关闭具体浏览器。
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional

from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state

# 同步等待定期复核调用方取消和进程停止；异步等待不占用工作线程。
_CANCEL_CHECK_INTERVAL = 0.05
_cancel_check: ContextVar[Optional[Callable[[], bool]]] = ContextVar("browser_cancel_check", default=None)
_queue_timeout: ContextVar[Optional[float]] = ContextVar("browser_queue_timeout", default=None)


@dataclass(frozen=True)
class BrowserQueueStatus:
    """
    当前进程内浏览器实例队列的只读状态快照。

    :param active: 当前尚未归还的浏览器实例名额数
    :param waiting: 当前正在排队等待准入的调用数
    :param limit: 当前生效的同时存活浏览器实例上限
    """

    active: int
    waiting: int
    limit: int

    @property
    def available(self) -> int:
        """
        返回当前可立即准入的空闲名额数；降容期间活跃实例超过上限时返回 0。

        :return: 空闲浏览器实例名额数，不小于 0
        """
        return max(0, self.limit - self.active)


class BrowserQueueCancelledError(RuntimeError):
    """
    浏览器尚未启动时，所属调用或进程已经停止。
    """


class BrowserOperationTimeoutError(RuntimeError):
    """
    浏览器启动、操作或关闭未在期限内收敛，动作的实际结果可能未知。
    """


def browser_cancel_check() -> Callable[[], bool]:
    """
    捕获当前请求的取消检查，供专属监控线程读取，不跨线程进入调用方上下文。
    """
    cancelled = _cancel_check.get()
    return lambda: runtime_stop_state.is_system_stopped or (cancelled is not None and cancelled())


def check_browser_request() -> None:
    """
    在等待和启动边界拒绝已经取消的调用，避免取消后继续创建浏览器。
    """
    cancelled = _cancel_check.get()
    if runtime_stop_state.is_system_stopped or (cancelled is not None and cancelled()):
        raise BrowserQueueCancelledError("浏览器请求已取消或系统正在停止")


def resolve_browser_queue_timeout(default: float) -> float:
    """
    解析当前调用生效的浏览器排队超时秒数。

    调用方通过 :func:`browser_request` 指定的超时只能收紧等待上限，不能突破宿主配置。

    :param default: 宿主配置的排队超时上限（秒）
    :return: 当前调用实际生效的排队超时秒数
    """
    override = _queue_timeout.get()
    if override is None:
        return default
    return min(default, override)


@contextmanager
def browser_request(
    cancelled: Optional[Callable[[], bool]] = None,
    *,
    queue_timeout: Optional[float] = None,
) -> Iterator[None]:
    """
    叠加当前调用的取消检查与可选排队超时上限，并随执行上下文传播到浏览器所属线程。

    在排队等待、启动和原生操作期间，只要 ``cancelled()`` 返回 ``True`` 或进程进入停机状态，
    尚未准入的调用会立即撤销排队票据并抛出 :class:`BrowserQueueCancelledError`，不影响其他持有者。
    嵌套使用时，任一外层取消条件触发都会使内层失效；``queue_timeout`` 只取更严格的上限。

    :param cancelled: 返回当前调用是否已失效的线程安全无参可调用对象；为 None 时沿用外层与停机检查
    :param queue_timeout: 本次调用允许的最长排队秒数，必须为有限正数；为 None 时沿用外层或宿主上限
    :raises TypeError: ``cancelled`` 非空且不可调用
    :raises ValueError: ``queue_timeout`` 不是有限正数
    """
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled 必须是无参可调用对象")
    if queue_timeout is not None and (
        isinstance(queue_timeout, bool)
        or not math.isfinite(queue_timeout)
        or queue_timeout <= 0
    ):
        raise ValueError("浏览器排队超时必须大于零")

    cancel_token: Optional[Token[Optional[Callable[[], bool]]]] = None
    timeout_token: Optional[Token[Optional[float]]] = None

    if cancelled is not None:
        parent = _cancel_check.get()

        def is_cancelled() -> bool:
            """
            任一祖先调用取消后，嵌套调用都不能再取得浏览器名额。
            """
            return cancelled() or (parent is not None and parent())

        cancel_token = _cancel_check.set(is_cancelled)

    if queue_timeout is not None:
        parent_timeout = _queue_timeout.get()
        effective_timeout = (
            float(queue_timeout)
            if parent_timeout is None
            else min(parent_timeout, float(queue_timeout))
        )
        timeout_token = _queue_timeout.set(effective_timeout)

    try:
        yield
    finally:
        if timeout_token is not None:
            _queue_timeout.reset(timeout_token)
        if cancel_token is not None:
            _cancel_check.reset(cancel_token)


class BrowserLease:
    """
    表示一个已经获准启动的浏览器实例；重复归还不会扩大容量。
    """

    def __init__(self, capacity: BrowserCapacity, ticket: object) -> None:
        """
        保存准入来源及唯一票据，不持有具体浏览器对象。

        :param capacity: 发放本名额的进程级队列
        :param ticket: 该名额唯一的身份票据
        """
        self._capacity = capacity
        self._ticket = ticket

    def release(self) -> None:
        """
        在浏览器确认关闭或启动失败后幂等归还名额。
        """
        self._capacity.release(self._ticket)


class BrowserCapacity:
    """
    同步线程和异步调用共享的先到先得浏览器队列。

    名额覆盖实例的完整存活期；等待超时和取消只移除自身票据，不影响持有者。
    """

    def __init__(self) -> None:
        """
        创建默认单实例队列，不启动线程、任务或浏览器。
        """
        self._condition = threading.Condition()
        self._limit = 1
        self._configured = False
        self._waiting: deque[object] = deque()
        self._active: set[object] = set()

    @property
    def active(self) -> int:
        """
        返回尚未归还的实例名额数。
        """
        with self._condition:
            return len(self._active)

    @property
    def waiting(self) -> int:
        """
        返回正在等待准入的调用数。
        """
        with self._condition:
            return len(self._waiting)

    @property
    def limit(self) -> int:
        """
        返回当前队列生效的同时存活实例上限。
        """
        with self._condition:
            return self._limit

    @property
    def configured(self) -> bool:
        """
        返回队列并发上限是否已按宿主配置初始化。
        """
        with self._condition:
            return self._configured

    def snapshot(self, *, default_limit: Optional[int] = None) -> BrowserQueueStatus:
        """
        原子读取当前活跃实例数、排队等待数与生效并发上限。

        :param default_limit: 队列尚未按宿主配置初始化时使用的展示上限；已配置时忽略该值
        :return: 只读队列状态快照
        """
        with self._condition:
            effective_limit = (
                self._limit
                if self._configured or default_limit is None
                else default_limit
            )
            return BrowserQueueStatus(
                active=len(self._active),
                waiting=len(self._waiting),
                limit=effective_limit,
            )

    def configure(self, limit: int) -> None:
        """
        更新后续准入容量；调低容量不撤销已经运行的实例。

        :param limit: 同时存活的浏览器实例上限，必须大于零
        """
        if limit < 1:
            raise ValueError("浏览器并发上限必须大于零")
        with self._condition:
            self._limit = limit
            self._configured = True
            self._condition.notify_all()

    def _enter(self) -> object:
        """
        登记唯一等待票据，统一同步和异步调用的先后顺序。
        """
        check_browser_request()
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
        return ticket

    def _take(self, ticket: object, deadline: float) -> bool:
        """
        在同一把锁内复核取消、超时、顺序和容量，再发放名额。
        """
        check_browser_request()
        if time.monotonic() >= deadline:
            raise TimeoutError("等待浏览器名额超时，已有实例仍被占用，请稍后重试")
        if self._waiting[0] is ticket and len(self._active) < self._limit:
            self._waiting.popleft()
            self._active.add(ticket)
            self._condition.notify_all()
            return True
        return False

    def _discard(self, ticket: object) -> None:
        """
        撤销尚未准入的调用，不能归还其它调用仍持有的名额。
        """
        with self._condition:
            if ticket in self._waiting:
                self._waiting.remove(ticket)
                self._condition.notify_all()

    def acquire(self, timeout: float) -> BrowserLease:
        """
        在同步线程有限等待浏览器名额。

        :param timeout: 最长排队秒数，必须大于零
        :return: 由实例关闭流程负责归还的名额
        """
        deadline = time.monotonic() + timeout
        ticket = self._enter()
        try:
            with self._condition:
                while not self._take(ticket, deadline):
                    self._condition.wait(min(_CANCEL_CHECK_INTERVAL, max(0.0, deadline - time.monotonic())))
            return BrowserLease(self, ticket)
        finally:
            self._discard(ticket)

    async def acquire_async(self, timeout: float) -> BrowserLease:
        """
        在事件循环内等待同一队列，取消时立即撤销票据，不遗留后台取号线程。

        :param timeout: 最长排队秒数，必须大于零
        :return: 由实例关闭流程负责归还的名额
        """
        deadline = time.monotonic() + timeout
        ticket = self._enter()
        try:
            while True:
                with self._condition:
                    if self._take(ticket, deadline):
                        return BrowserLease(self, ticket)
                await asyncio.sleep(min(_CANCEL_CHECK_INTERVAL, max(0.0, deadline - time.monotonic())))
        finally:
            self._discard(ticket)

    def release(self, ticket: object) -> None:
        """
        只有对应实例的票据可以释放自己的名额，关闭重试保持幂等。
        """
        with self._condition:
            self._active.discard(ticket)
            self._condition.notify_all()


browser_capacity = BrowserCapacity()


def get_browser_queue_status() -> BrowserQueueStatus:
    """
    查询当前进程内浏览器实例队列的只读状态快照。

    本接口不会修改队列状态、占用名额或加载浏览器内核，适合在发起低优先级任务前评估排队深度。

    :return: 包含活跃实例数、排队等待数、并发上限与空闲名额数的只读快照
    """
    if browser_capacity.configured:
        return browser_capacity.snapshot()
    configured_limit = max(1, int(get_runtime_setting("CLOAKBROWSER_MAX_CONCURRENCY", 3)))
    return browser_capacity.snapshot(default_limit=configured_limit)
