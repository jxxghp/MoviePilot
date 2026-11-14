"""
交互轮次的执行归属、临时 HTTP 凭据和后台协作避让。

仅宿主入口可以建立作用域；复制的上下文在轮次结束后自动失效。
此处不抢占已开始的线程或事务，也不改变用户鉴权。
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from anyio import CapacityLimiter
from anyio.lowlevel import RunVar
from anyio.to_thread import current_default_thread_limiter

from app.runtime.settings import get_runtime_setting

PRIORITY_HEADER = "X-MoviePilot-Execution"
INTERACTIVE_THREAD_WORKERS = 4
INTERACTIVE_DATABASE_WORKERS = 1
INTERACTIVE_DATABASE_CAPACITY = 8
_condition = threading.Condition()


@dataclass
class _InteractiveScope:
    """
    记录一次可信交互的存活期。

    :param key: 仅在本进程存活期间有效的随机 HTTP 传播凭据
    :param active: 是否仍处于原始交互轮次内
    """

    key: str
    active: bool = True


_current: ContextVar[Optional[_InteractiveScope]] = ContextVar("interactive_execution", default=None)
_scopes: dict[str, _InteractiveScope] = {}
# RunVar 按事件循环而非请求保存 limiter，避免不同会话各建一份配额。
_limiter: RunVar[CapacityLimiter] = RunVar("interactive_thread_limiter")
_background_limiter: RunVar[CapacityLimiter] = RunVar("background_thread_limiter")


def is_interactive() -> bool:
    """
    返回当前调用是否属于仍存活且已启用优先策略的交互轮次。
    """
    scope = _current.get()
    return bool(scope and scope.active and get_runtime_setting("AGENT_INTERACTIVE_PRIORITY", True))


def interactive_key() -> Optional[str]:
    """
    返回供本机 API 传播使用的临时凭据；普通后台调用返回 None。
    """
    scope = _current.get()
    return scope.key if scope and is_interactive() else None


@contextmanager
def interactive_execution(enabled: bool = True) -> Iterator[None]:
    """
    为宿主确认的交互轮次登记归属，异常和取消均撤销凭据。

    :param enabled: False 时隔离继承的上下文，供自主任务入口使用
    """
    if enabled and is_interactive():
        yield
        return
    scope = _InteractiveScope(uuid.uuid4().hex) if enabled else None
    token = _current.set(scope)
    if scope is not None:
        with _condition:
            _scopes[scope.key] = scope
    try:
        yield
    finally:
        if scope is not None:
            with _condition:
                scope.active = False
                _scopes.pop(scope.key, None)
                _condition.notify_all()
        _current.reset(token)


@contextmanager
def propagated_execution(key: Optional[str]) -> Iterator[None]:
    """
    仅恢复本进程登记且尚未结束的交互凭据，伪造或过期值按普通调用处理。
    """
    with _condition:
        scope = _scopes.get(key or "")
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


def interactive_thread_limiter() -> CapacityLimiter:
    """
    返回当前事件循环共享的交互线程配额，不消费 anyio 默认后台令牌。
    """
    limiter = _limiter.get(None)
    if limiter is None:
        limiter = CapacityLimiter(INTERACTIVE_THREAD_WORKERS)
        _limiter.set(limiter)
    return limiter


def background_thread_limiter() -> CapacityLimiter:
    """
    隔离宿主后台同步调用，避免其耗尽 FastAPI 同步鉴权和端点的默认令牌。

    普通容量跟随 anyio 默认配置；不修改框架 limiter 或第三方库的线程入口。
    """
    capacity = current_default_thread_limiter().total_tokens
    limiter = _background_limiter.get(None)
    if limiter is None:
        limiter = CapacityLimiter(capacity)
        _background_limiter.set(limiter)
    else:
        limiter.total_tokens = capacity
    return limiter


def background_defer_seconds() -> float:
    """
    返回非关键后台任务的最长避让秒数；当前交互主动触发的调用不自我等待。
    """
    if is_interactive() or not get_runtime_setting("AGENT_INTERACTIVE_PRIORITY", True):
        return 0.0
    return float(get_runtime_setting("AGENT_BACKGROUND_DEFER_SECONDS", 30))


def _busy() -> bool:
    """
    返回是否仍有交互轮次在思考或执行工具。
    """
    with _condition:
        return bool(_scopes)


def wait_for_interactive_idle(timeout: float, accepting: Callable[[], bool]) -> None:
    """
    同步后台入口有限避让；按短间隔检查关闭，避免关停等待整段预算。
    """
    deadline = time.monotonic() + timeout
    while _busy() and accepting():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with _condition:
            _condition.wait(min(remaining, 0.05))


async def await_interactive_idle(timeout: float, accepting: Callable[[], bool]) -> None:
    """
    协程后台入口有限避让，等待不占用主循环且保留取消传播。
    """
    deadline = time.monotonic() + timeout
    while _busy() and accepting():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 0.05))
