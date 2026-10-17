"""
同步与异步防抖调度，合并短时间内的重复调用并支持取消待执行任务。
"""

import asyncio
import functools
import inspect
from abc import ABC, abstractmethod
from threading import Lock, Timer
from typing import Awaitable, Callable, Concatenate, Generic, Optional, ParamSpec, Protocol, TypeVar, cast, overload

from app.runtime.log import logger

_P = ParamSpec("_P")
_BoundP = ParamSpec("_BoundP")
_R = TypeVar("_R")
_T = TypeVar("_T")


class _Debounced(Protocol[_P]):
    """
    保留调用参数；实际函数种类决定调用与取消是否返回可等待对象。

    :ivar cancel: 取消挂起调用的控制方法；异步引擎返回需等待的清理协程
    """

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> Awaitable[None] | None:
        """
        提交本次调用；同步函数直接返回，协程函数需要等待提交。

        :param args: 传给被包装函数的位置参数，后续调用可替换尚未执行的参数
        :param kwargs: 传给被包装函数的关键字参数，后续调用可替换尚未执行的参数
        :return: 同步引擎提交后返回 None；异步引擎返回需要等待的提交协程
        """
        ...

    cancel: Callable[[], Awaitable[None] | None]

    @overload
    def __get__(self, instance: None, owner: type[object] | None = None) -> "_Debounced[_P]":
        """
        从类访问时保留尚未绑定的参数。

        :param instance: 访问此描述符的实例；从类访问时为 None
        :param owner: 访问此描述符的类；未显式提供时为 None
        :return: 从类访问时返回未绑定包装器；从实例访问时返回已绑定首个参数的包装器
        """
        ...

    @overload
    def __get__(self: "_Debounced[Concatenate[_T, _BoundP]]", instance: _T,
                owner: type[_T] | None = None) -> "_Debounced[_BoundP]":
        """
        从实例访问时由函数描述符绑定首个参数。

        :param instance: 访问此描述符的实例；从类访问时为 None
        :param owner: 访问此描述符的类；未显式提供时为 None
        :return: 从类访问时返回未绑定包装器；从实例访问时返回已绑定首个参数的包装器
        """
        ...


class BaseDebouncer(ABC, Generic[_P, _R]):
    """
    防抖器的抽象基类。定义了防抖器的基本接口和日志功能。
    所有防抖器实现类必须继承此类并实现其抽象方法。
    """

    def __init__(self, func: Callable[_P, _R], interval: float, *,
                 leading: bool = False, enable_logging: bool = False, source: str = "") -> None:
        """
        初始化防抖器实例。

        :param func: 要防抖的函数或协程
        :param interval: 防抖间隔，单位秒
        :param leading: 是否启用前沿模式
        :param enable_logging: 是否启用日志记录
        :param source: 日志来源标识
        """
        self.func = func
        self.interval = interval
        self.leading = leading
        self.enable_logging = enable_logging
        self.source = source

    @abstractmethod
    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> Awaitable[None] | None:
        """
        定义防抖调用的契约，子类必须实现。
        """
        pass

    @abstractmethod
    def cancel(self) -> Awaitable[None] | None:
        """
        定义取消挂起调用的契约，子类必须实现。
        """
        pass

    def format_log(self, message: str) -> str:
        """
        格式化日志消息，加入 source 前缀。
        """
        return f"[{self.source}] {message}" if self.source else message

    def log(self, level: str, message: str) -> None:
        """
        根据日志级别记录日志。
        """
        if self.enable_logging:
            log_method = getattr(logger, level, logger.debug)
            log_method(self.format_log(message))

    def log_debug(self, message: str) -> None:
        """
        记录调试日志。
        """
        self.log("debug", message)

    def log_info(self, message: str) -> None:
        """
        记录信息日志。
        """
        self.log("info", message)

    def log_warning(self, message: str) -> None:
        """
        记录警告日志。
        """
        self.log("warning", message)

    def error(self, message: str) -> None:
        """
        记录错误日志。
        """
        self.log("error", message)

    def critical(self, message: str) -> None:
        """
        记录严重错误日志。
        """
        self.log("critical", message)


class Debouncer(BaseDebouncer[_P, object]):
    """
    在线程计时器中执行同步防抖调用，支持立即执行首个调用或延迟执行最后一个调用。
    """

    def __init__(self, func: Callable[_P, object], interval: float, *,
                 leading: bool = False, enable_logging: bool = False, source: str = "") -> None:
        """
        初始化防抖器实例。
        """
        super().__init__(func, interval, leading=leading, enable_logging=enable_logging, source=source)
        self.timer: Optional[Timer] = None
        self.lock = Lock()
        # 用于前沿模式，标记是否处于“冷却”或“不应期”
        self.is_cooling_down = False

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        提交防抖调用，后续调用覆盖尚未执行的参数。

        :param args: 传给原函数的位置参数
        :param kwargs: 传给原函数的关键字参数
        """
        with self.lock:
            if self.leading:
                self._call_leading(*args, **kwargs)
            else:
                self._call_trailing(*args, **kwargs)

    def _call_leading(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        冷却结束后的首个调用立即执行，每次调用都会重新开始冷却计时。
        """
        if not self.is_cooling_down:
            self.log_info("前沿模式: 立即执行函数。")
            self.func(*args, **kwargs)

        # 无论是否执行，都重置冷却计时器
        if self.timer and self.timer.is_alive():
            self.timer.cancel()

        self.is_cooling_down = True

        self.timer = Timer(self.interval, self._end_cool_down)
        self.timer.start()
        self.log_debug(f"前沿模式: 进入 {self.interval} 秒的冷却期。")

    def _end_cool_down(self) -> None:
        """
        计时器到期后，解除冷却状态。
        """
        with self.lock:
            self.is_cooling_down = False
            self.log_debug("前沿模式: 冷却时间结束，可以再次立即执行。")

    def _call_trailing(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        取消旧计时器并延后执行，使静默间隔结束后只执行最新调用。
        """
        if self.timer and self.timer.is_alive():
            self.timer.cancel()
            self.log_debug("后沿模式: 检测到新的调用，已重置计时器。")

        def execute() -> None:
            """
            计时结束后执行最后一次调用保存的参数。
            """
            self.log_info("后沿模式: 计时结束，开始执行函数。")
            self.func(*args, **kwargs)

        self.timer = Timer(self.interval, execute)
        self.timer.start()
        self.log_debug(f"后沿模式: 计时器已启动，将在 {self.interval} 秒后执行。")

    def cancel(self) -> None:
        """
        取消任何挂起的调用，并重置状态。
        """
        with self.lock:
            if self.timer and self.timer.is_alive():
                self.timer.cancel()
                self.timer = None
                self.log_info("防抖器被手动取消。")
            self.is_cooling_down = False


class AsyncDebouncer(BaseDebouncer[_P, Awaitable[object]]):
    """
    在事件循环中执行协程防抖调用，并持有被取消的任务直到其真正结束。
    """

    def __init__(self, func: Callable[_P, Awaitable[object]], interval: float, *,
                 leading: bool = False, enable_logging: bool = False, source: str = "") -> None:
        """
        初始化异步防抖器实例。
        """
        super().__init__(func, interval, leading=leading, enable_logging=enable_logging, source=source)
        self.task: Optional[asyncio.Task[None]] = None
        self._retired_tasks: set[asyncio.Task[None]] = set()
        self.lock = asyncio.Lock()
        self.is_cooling_down = False

    def _cancel_active_task(self) -> None:
        """
        取消当前任务并保留 owner，直到任务进入真实终态。
        """
        task = self.task
        if task is None or task.done():
            return
        task.cancel()
        self._retired_tasks.add(task)
        task.add_done_callback(self._retired_tasks.discard)

    async def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        异步调用防抖函数。
        """
        async with self.lock:
            if self.leading:
                await self._call_leading(*args, **kwargs)
            else:
                await self._call_trailing(*args, **kwargs)

    async def _call_leading(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        等待冷却结束后的首个协程执行，再为本次调用重新开始冷却计时。
        """
        if not self.is_cooling_down:
            self.log_info("前沿模式 (async): 立即执行协程。")
            await self.func(*args, **kwargs)

        self._cancel_active_task()

        self.is_cooling_down = True
        self.task = asyncio.create_task(self._end_cool_down())
        self.log_debug(f"前沿模式 (async): 进入 {self.interval} 秒的冷却期。")

    async def _end_cool_down(self) -> None:
        """
        计时器到期后，解除冷却状态。
        """
        await asyncio.sleep(self.interval)
        async with self.lock:
            self.is_cooling_down = False
            self.log_debug("前沿模式 (async): 冷却时间结束。")

    async def _call_trailing(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        取消并保留旧任务的清理责任，创建携带最新参数的延迟任务。
        """
        if self.task and not self.task.done():
            self._cancel_active_task()
            self.log_debug("后沿模式 (async): 检测到新的调用，已取消旧任务。")

        self.task = asyncio.create_task(self._delayed_execute(*args, **kwargs))
        self.log_debug(f"后沿模式 (async): 任务已创建，将在 {self.interval} 秒后执行。")

    async def _delayed_execute(self, *args: _P.args, **kwargs: _P.kwargs) -> None:
        """
        延迟执行实际的协程函数。
        """
        try:
            await asyncio.sleep(self.interval)
            self.log_info("后沿模式 (async): 延迟结束，开始执行协程。")
            await self.func(*args, **kwargs)
        except asyncio.CancelledError:
            # 任务被取消是正常行为，无需处理
            pass

    async def cancel(self) -> None:
        """
        取消并等待所有挂起调用进入终态，再重置状态。
        """
        async with self.lock:
            current_task = asyncio.current_task()
            tasks = {
                task
                for task in (*self._retired_tasks, self.task)
                if task is not None and task is not current_task
            }
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._retired_tasks.difference_update(tasks)
            self.task = None
            if tasks:
                self.log_info("异步防抖器被手动取消。")
            self.is_cooling_down = False


def debounce(interval: float, *, leading: bool = False,
             enable_logging: bool = False, source: str = "") -> Callable[[Callable[_P, object]], _Debounced[_P]]:
    """
    支持同步和异步的防抖装饰器工厂。
    """

    def decorator(func: Callable[_P, object]) -> _Debounced[_P]:
        """
        按实际协程函数标记选择引擎，保留原函数的参数与绑定行为。
        """
        if inspect.iscoroutinefunction(func):
            async_instance = AsyncDebouncer(func, interval,
                                            leading=leading,
                                            enable_logging=enable_logging,
                                            source=source)

            @functools.wraps(func)
            async def async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
                """
                等待异步引擎接收本次调用。
                """
                await async_instance(*args, **kwargs)

            async_result = cast(_Debounced[_P], async_wrapper)
            async_result.cancel = async_instance.cancel
            return async_result

        else:
            instance = Debouncer(func, interval,
                                 leading=leading,
                                 enable_logging=enable_logging,
                                 source=source)

            @functools.wraps(func)
            def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
                """
                通过同步引擎接收本次调用。
                """
                instance(*args, **kwargs)

            result = cast(_Debounced[_P], wrapper)
            result.cancel = instance.cancel
            return result

    return decorator
