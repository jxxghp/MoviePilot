"""旧插件专用的防抖器与防抖装饰器。

本模块只为旧插件而存在：宿主自身无任何调用方，仅由 ``app.utils.debounce``
兼容别名指向。宿主新代码不得导入本模块。
"""

import asyncio
import functools
import inspect
from abc import ABC, abstractmethod
from threading import Timer, Lock
from typing import Callable, Any, Optional

from app.runtime.log import logger


class BaseDebouncer(ABC):
    """
    防抖器的抽象基类。定义了防抖器的基本接口和日志功能。
    所有防抖器实现类必须继承此类并实现其抽象方法。
    """
    def __init__(self, func: Callable, interval: float, *,
                 leading: bool = False, enable_logging: bool = False, source: str = ""):
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
    def __call__(self, *args, **kwargs) -> None:
        """
        定义防抖调用的契约，子类必须实现。
        """
        pass

    @abstractmethod
    def cancel(self) -> None:
        """
        定义取消挂起调用的契约，子类必须实现。
        """
        pass

    def format_log(self, message: str) -> str:
        """
        格式化日志消息，加入 source 前缀。
        """
        return f"[{self.source}] {message}" if self.source else message

    def log(self, level: str, message: str):
        """
        根据日志级别记录日志。
        """
        if self.enable_logging:
            log_method = getattr(logger, level, logger.debug)
            log_method(self.format_log(message))

    def log_debug(self, message: str):
        """
        记录调试日志。
        """
        self.log("debug", message)

    def log_info(self, message: str):
        """
        记录信息日志。
        """
        self.log("info", message)

    def log_warning(self, message: str):
        """
        记录警告日志。
        """
        self.log("warning", message)

    def error(self, message: str):
        """
        记录错误日志。
        """
        self.log("error", message)

    def critical(self, message: str):
        """
        记录严重错误日志。
        """
        self.log("critical", message)


class Debouncer(BaseDebouncer):
    """
    同步防抖实现类
    """

    def __init__(self, *args, **kwargs):
        """
        初始化防抖器实例。
        """
        super().__init__(*args, **kwargs)
        self.timer: Optional[Timer] = None
        self.lock = Lock()
        # 用于前沿模式，标记是否处于“冷却”或“不应期”
        self.is_cooling_down = False

    def __call__(self, *args, **kwargs) -> None:
        """
        调用防抖函数。
        :param args:
        :param kwargs:
        :return:
        """
        with self.lock:
            if self.leading:
                self._call_leading(*args, **kwargs)
            else:
                self._call_trailing(*args, **kwargs)

    def _call_leading(self, *args, **kwargs):
        """
        前沿模式的逻辑。
        """
        # 如果不在冷却期，则立即执行
        if not self.is_cooling_down:
            self.log_info("前沿模式: 立即执行函数。")
            self.func(*args, **kwargs)

        # 无论是否执行，都重置冷却计时器
        if self.timer and self.timer.is_alive():
            self.timer.cancel()

        # 设置自己进入冷却期
        self.is_cooling_down = True

        # 在间隔结束后，将冷却状态解除
        self.timer = Timer(self.interval, self._end_cool_down)
        self.timer.start()
        self.log_debug(f"前沿模式: 进入 {self.interval} 秒的冷却期。")

    def _end_cool_down(self):
        """
        计时器到期后，解除冷却状态
        """
        with self.lock:
            self.is_cooling_down = False
            self.log_debug("前沿模式: 冷却时间结束，可以再次立即执行。")

    def _call_trailing(self, *args, **kwargs):
        """
        后沿模式的逻辑。
        """
        # 【日志点】记录计时器被重置
        if self.timer and self.timer.is_alive():
            self.timer.cancel()
            self.log_debug("后沿模式: 检测到新的调用，已重置计时器。")

        def execute():
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


class AsyncDebouncer(BaseDebouncer):
    """
    异步防抖实现类。
    """
    def __init__(self, *args, **kwargs):
        """
        初始化异步防抖器实例。
        """
        super().__init__(*args, **kwargs)
        self.task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.is_cooling_down = False

    async def __call__(self, *args, **kwargs) -> None:
        """
        异步调用防抖函数。
        """
        async with self.lock:
            if self.leading:
                await self._call_leading(*args, **kwargs)
            else:
                await self._call_trailing(*args, **kwargs)

    async def _call_leading(self, *args, **kwargs):
        """
        前沿模式的逻辑。
        """
        if not self.is_cooling_down:
            self.log_info("前沿模式 (async): 立即执行协程。")
            await self.func(*args, **kwargs)

        if self.task and not self.task.done():
            self.task.cancel()

        self.is_cooling_down = True
        self.task = asyncio.create_task(self._end_cool_down())
        self.log_debug(f"前沿模式 (async): 进入 {self.interval} 秒的冷却期。")

    async def _end_cool_down(self):
        """
        计时器到期后，解除冷却状态
        """
        await asyncio.sleep(self.interval)
        async with self.lock:
            self.is_cooling_down = False
            self.log_debug("前沿模式 (async): 冷却时间结束。")

    async def _call_trailing(self, *args, **kwargs):
        """
        后沿模式的逻辑。
        """
        if self.task and not self.task.done():
            self.task.cancel()
            self.log_debug("后沿模式 (async): 检测到新的调用，已取消旧任务。")

        self.task = asyncio.create_task(self._delayed_execute(*args, **kwargs))
        self.log_debug(f"后沿模式 (async): 任务已创建，将在 {self.interval} 秒后执行。")

    async def _delayed_execute(self, *args, **kwargs):
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
        取消任何挂起的调用，并重置状态。
        """
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                self.task = None
                self.log_info("异步防抖器被手动取消。")
            self.is_cooling_down = False


def debounce(interval: float, *, leading: bool = False,
             enable_logging: bool = False, source: str = "") -> Callable:
    """
    支持同步和异步的防抖装饰器工厂。
    """

    def decorator(func: Callable) -> Callable:
        # 检查函数类型，并选择合适的引擎
        if inspect.iscoroutinefunction(func):
            # 异步函数，使用 AsyncDebouncer
            instance = AsyncDebouncer(func, interval,
                                      leading=leading,
                                      enable_logging=enable_logging,
                                      source=source)

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                await instance(*args, **kwargs)

            async_wrapper.cancel = instance.cancel
            return async_wrapper

        else:
            # 同步函数，使用 Debouncer
            instance = Debouncer(func, interval,
                                 leading=leading,
                                 enable_logging=enable_logging,
                                 source=source)

            @functools.wraps(func)
            def wrapper(*args, **kwargs) -> Any:
                instance(*args, **kwargs)

            wrapper.cancel = instance.cancel
            return wrapper

    return decorator
