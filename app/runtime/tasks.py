"""进程内后台任务登记与生命周期收口。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """记录一个后台任务的所有者，便于关停阶段按责任域收口。"""

    owner: str
    task: asyncio.Task[Any]
    cancel_on_shutdown: bool


class TaskRegistry:
    """管理由宿主创建的进程内后台任务，并提供统一取消与等待入口。"""

    def __init__(self) -> None:
        """初始化空任务登记表。"""
        self._records: dict[asyncio.Task[Any], TaskRecord] = {}
        self._accepting = True

    @property
    def records(self) -> tuple[TaskRecord, ...]:
        """返回当前仍未完成的任务快照。"""
        return tuple(
            record for record in self._records.values() if not record.task.done()
        )

    def create(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> asyncio.Task[Any]:
        """创建并登记后台任务，任务完成后自动从登记表移除。"""
        if not self._accepting:
            coroutine.close()
            raise RuntimeError("后台任务登记器正在关闭，不能再创建新任务")
        task = asyncio.create_task(coroutine, name=owner)
        self.register(
            task,
            owner=owner,
            cancel_on_shutdown=cancel_on_shutdown,
        )
        return task

    def create_sync(
        self,
        function: Callable[..., Any],
        *args: Any,
        owner: str,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        """在线程池执行同步后台函数并登记其异步生命周期。"""
        return self.create(
            asyncio.to_thread(partial(function, *args, **kwargs)),
            owner=owner,
            cancel_on_shutdown=False,
        )

    def register(
        self,
        task: asyncio.Task[Any],
        *,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> asyncio.Task[Any]:
        """登记已有任务并绑定责任域。"""
        if not self._accepting:
            task.cancel()
            raise RuntimeError("后台任务登记器正在关闭，不能再登记新任务")
        task.set_name(owner)
        self._records[task] = TaskRecord(
            owner=owner,
            task=task,
            cancel_on_shutdown=cancel_on_shutdown,
        )
        task.add_done_callback(self._discard)
        return task

    def _discard(self, task: asyncio.Task[Any]) -> None:
        """移除已结束任务，并把未处理异常交给事件循环统一报告。"""
        record = self._records.pop(task, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            task.get_loop().call_exception_handler(
                {
                    "message": "MoviePilot 后台任务执行失败",
                    "exception": exception,
                    "task": task,
                    "owner": record.owner if record else task.get_name(),
                }
            )

    async def shutdown(self, *, timeout_seconds: float = 10.0) -> None:
        """取消并等待全部登记任务，超时后放弃等待但不影响其他关闭步骤。"""
        self._accepting = False
        records = self.records
        tasks = [record.task for record in records]
        for record in records:
            if record.cancel_on_shutdown:
                record.task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            for task in pending:
                task.cancel()
        self._records.clear()


_default_registry = TaskRegistry()
_runtime_registry: TaskRegistry | None = None


def configure_task_registry(registry: TaskRegistry | None) -> None:
    """由启动组合根发布当前 lifespan 的任务登记器。"""
    global _runtime_registry
    _runtime_registry = registry


def get_task_registry() -> TaskRegistry:
    """返回当前宿主任务登记器，未启动完整 lifespan 时保留测试兼容回退。"""
    return _runtime_registry or _default_registry
