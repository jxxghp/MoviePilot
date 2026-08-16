"""插件可依赖的轻量浏览器启动接口。"""

from __future__ import annotations

from typing import Any


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


__all__ = ["launch_browser_context", "launch_browser_context_async"]
