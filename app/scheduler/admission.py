"""
非关键调度任务的有限避让，保留原调度所有权和重入保护。
"""

import asyncio
from collections.abc import Callable
from typing import Any

from app.runtime.priority import (
    await_interactive_idle,
    background_defer_seconds,
    interactive_execution,
    wait_for_interactive_idle,
)

# 安全检查、持久副作用补偿与生命周期维护不得等待用户对话结束。
_CRITICAL_OWNERS = frozenset({"outbox", "security", "runtime", "database"})


def _defer_budget(job: dict[str, Any]) -> float:
    """
    只对自动业务任务避让，手动专用任务和关键维护保留立即执行语义。
    """
    if job.get("manual") or job.get("owner") in _CRITICAL_OWNERS:
        return 0.0
    return background_defer_seconds()


async def defer_coroutine(job: dict[str, Any], accepting: Callable[[], bool]) -> None:
    """
    协程等待期间持续持有调度句柄，取消或关闭仍由原 owner 收口。
    """
    job["deferred"] = True
    try:
        await await_interactive_idle(_defer_budget(job), accepting)
        if not accepting():
            raise asyncio.CancelledError()
    finally:
        job["deferred"] = False


def invoke_background(
    job: dict[str, Any], accepting: Callable[[], bool], func: Callable[..., Any],
    *args: Any, **kwargs: Any,
) -> Any:
    """
    在线程入口有限避让后以普通执行归属调用业务函数。

    已在事件循环中的旧同步入口不能阻塞等待；这类入口沿用原立即调用合同。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        job["deferred"] = True
        try:
            wait_for_interactive_idle(_defer_budget(job), accepting)
        finally:
            job["deferred"] = False
    if not accepting():
        raise RuntimeError("调度器正在关闭，尚未开始的任务已取消")
    with interactive_execution(enabled=False):
        return func(*args, **kwargs)
