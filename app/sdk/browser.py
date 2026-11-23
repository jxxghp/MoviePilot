"""
插件可依赖的轻量浏览器启动、排队状态查询与取消边界接口。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

from app.runtime.browser import (
    BrowserOperationTimeoutError,
    BrowserQueueCancelledError,
    BrowserQueueStatus,
    browser_request,
    get_browser_queue_status,
)


def launch_browser_context(headless: bool = True, **kwargs: Any) -> Any:
    """
    启动同步浏览器上下文，并由宿主协调所需进程资源。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    from app.adapters.network.browser import launch_browser_context as launch

    return launch(headless=headless, **kwargs)


async def launch_browser_context_async(headless: bool = True, **kwargs: Any) -> Any:
    """
    启动异步浏览器上下文，并由宿主协调所需进程资源。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    from app.adapters.network.browser import launch_browser_context_async as launch

    return await launch(headless=headless, **kwargs)


@contextmanager
def browser_context(
    headless: bool = True,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
    queue_timeout: Optional[float] = None,
    **kwargs: Any,
) -> Iterator[Any]:
    """
    在取消与排队超时边界内启动同步浏览器上下文，并在退出时关闭归还名额。

    :param headless: 是否使用无头模式
    :param cancelled: 返回当前任务是否已取消的线程安全无参可调用对象
    :param queue_timeout: 本次调用允许的最长排队秒数，必须为有限正数；超过宿主上限时按宿主上限生效
    :param kwargs: 浏览器实现接受的其余启动参数
    :yields: 同步浏览器上下文
    """
    from app.adapters.network.browser import browser_context as open_context

    with open_context(
        headless=headless,
        cancelled=cancelled,
        queue_timeout=queue_timeout,
        **kwargs,
    ) as context:
        yield context


@asynccontextmanager
async def browser_context_async(
    headless: bool = True,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
    queue_timeout: Optional[float] = None,
    **kwargs: Any,
) -> AsyncIterator[Any]:
    """
    在取消与排队超时边界内启动异步浏览器上下文，并在退出时关闭归还名额。

    :param headless: 是否使用无头模式
    :param cancelled: 返回当前任务是否已取消的线程安全无参可调用对象
    :param queue_timeout: 本次调用允许的最长排队秒数，必须为有限正数；超过宿主上限时按宿主上限生效
    :param kwargs: 浏览器实现接受的其余启动参数
    :yields: 异步浏览器上下文
    """
    from app.adapters.network.browser import browser_context_async as open_context

    async with open_context(
        headless=headless,
        cancelled=cancelled,
        queue_timeout=queue_timeout,
        **kwargs,
    ) as context:
        yield context


__all__ = [
    "BrowserOperationTimeoutError",
    "BrowserQueueCancelledError",
    "BrowserQueueStatus",
    "browser_context",
    "browser_context_async",
    "browser_request",
    "get_browser_queue_status",
    "launch_browser_context",
    "launch_browser_context_async",
]
