"""插件生命周期入口的低基数耗时观测。"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar, cast

from app.runtime.observability import record_metric
from app.schemas.plugin import PluginRuntimeStatus


P = ParamSpec("P")
R = TypeVar("R")


def observe_plugin_lifecycle(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """记录插件生命周期入口的耗时，指标标签不含插件标识。

    插件标识是高基数维度，进标签会让时序库按插件数量膨胀，因此只记录操作名与结果。
    返回值里出现 ``PluginRuntimeStatus.LOAD_FAILED`` 时结果记为 ``error``——生命周期
    入口以返回状态而非抛异常表达单个插件的失败，只看异常会把整批失败记成成功。

    :param operation: 生命周期操作名，作为指标标签
    :return: 保留原调用签名的装饰器
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """包装单个同步生命周期方法，并保留原始调用签名。

        :param func: 被包装的生命周期方法
        :return: 记录耗时后透传原返回值的包装函数
        """

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """执行生命周期方法并把失败状态归一为 error。

            :return: 原方法的返回值
            """
            started_at = time.perf_counter()
            outcome = "success"
            try:
                result = func(*args, **kwargs)
                statuses = result.values() if isinstance(result, dict) else (result,)
                if PluginRuntimeStatus.LOAD_FAILED in statuses:
                    outcome = "error"
                return result
            except BaseException:
                outcome = "error"
                raise
            finally:
                record_metric(
                    "plugin.lifecycle.duration",
                    time.perf_counter() - started_at,
                    operation=operation,
                    outcome=outcome,
                )

        return cast(Callable[P, R], wrapper)

    return decorator
