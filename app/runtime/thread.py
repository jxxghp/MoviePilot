from concurrent.futures import Future
from contextvars import copy_context
from typing import Any, Callable, TypeVar, cast

from app.foundation.singleton import Singleton
from app.runtime.execution import OwnedThreadPoolExecutor
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()

_Result = TypeVar("_Result")
_THREAD_POOL_STOP_TIMEOUT_SECONDS = 10.0

# 阶段 64 曾在本模块引入该私有名；保留精确别名，避免测试或外部诊断代码失效。
_OwnedThreadPoolExecutor = OwnedThreadPoolExecutor


# strict mypy 跳过 foundation 实现导入，因此无法在本文件解析既有 Singleton 元类类型。
class ThreadHelper(metaclass=Singleton):  # type: ignore[metaclass]
    """
    共享后台线程池 owner，负责关联上下文传播和生命周期收敛。
    """

    def __init__(self) -> None:
        """按系统配置创建共享后台线程池。"""
        self.pool = OwnedThreadPoolExecutor(max_workers=settings.CONF.threadpool)

    def submit(
            self,
            func: Callable[..., _Result],
            *args: Any,
            **kwargs: Any,
    ) -> Future[_Result]:
        """
        提交任务
        :param func: 函数
        :param args: 参数
        :param kwargs: 参数
        :return: future
        """
        context = copy_context()
        # strict mypy 跳过 execution 实现导入，需要在兼容门面恢复 Future 泛型。
        return cast(Future[_Result], self.pool.submit(context.run, func, *args, **kwargs))

    def shutdown(
            self,
            timeout: float = _THREAD_POOL_STOP_TIMEOUT_SECONDS,
    ) -> bool:
        """
        有限等待共享线程池关闭。

        :param timeout: 等待已接受任务达到终态的最长秒数
        :return: 全部任务和 worker 均已终止时返回 True，否则返回 False
        """
        return bool(self.pool.shutdown_bounded(timeout=timeout))
