"""进程内后台任务登记与生命周期收口。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
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


@dataclass(slots=True)
class _ThreadsafeSubmission:
    """持有跨线程提交从排队到真实 Task 发布之间的生命周期。"""

    coroutine: Coroutine[Any, Any, Any]
    completion: concurrent.futures.Future[Any]
    loop: asyncio.AbstractEventLoop
    owner: str
    cancel_on_shutdown: bool
    task: asyncio.Task[Any] | None = None
    coroutine_closed: bool = False


class TaskRegistry:
    """管理由宿主创建的进程内后台任务，并提供统一取消与等待入口。"""

    def __init__(self) -> None:
        """初始化空任务登记表。"""
        self._records: dict[asyncio.Task[Any], TaskRecord] = {}
        self._shutdown_cancel_requested: set[asyncio.Task[Any]] = set()
        self._shutdown_timeout_reported: set[asyncio.Task[Any]] = set()
        self._threadsafe_submissions: dict[
            concurrent.futures.Future[Any], _ThreadsafeSubmission
        ] = {}
        self._state_lock = threading.Lock()
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

    def submit_threadsafe(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        loop: asyncio.AbstractEventLoop,
        owner: str,
        cancel_on_shutdown: bool = True,
    ) -> concurrent.futures.Future[Any]:
        """从宿主线程提交协程，并持有排队阶段直至发布真实 Task。"""
        completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
        submission = _ThreadsafeSubmission(
            coroutine=coroutine,
            completion=completion,
            loop=loop,
            owner=owner,
            cancel_on_shutdown=cancel_on_shutdown,
        )

        def mirror_completion(task: asyncio.Task[Any]) -> None:
            """把登记任务的真实终态镜像给跨线程调用方。"""
            if completion.done():
                return
            try:
                if task.cancelled():
                    completion.cancel()
                    return
                exception = task.exception()
                if exception is not None:
                    completion.set_exception(exception)
                else:
                    completion.set_result(task.result())
            except concurrent.futures.InvalidStateError:
                # completion 可由调用线程同时取消，真实 Task 仍由 Registry 观察。
                return

        def submit_on_loop() -> None:
            """在目标循环内把 pending submission 原子移交给真实 Task。"""
            close_coroutine = False
            task: asyncio.Task[Any] | None = None
            error: Exception | None = None
            with self._state_lock:
                current = self._threadsafe_submissions.get(completion)
                if current is not submission:
                    return
                if completion.cancelled():
                    self._threadsafe_submissions.pop(completion, None)
                    submission.coroutine_closed = True
                    close_coroutine = True
                else:
                    try:
                        task = self.create(
                            coroutine,
                            owner=owner,
                            cancel_on_shutdown=cancel_on_shutdown,
                        )
                    except Exception as caught:
                        error = caught
                        submission.coroutine_closed = True
                        close_coroutine = True
                    else:
                        submission.task = task
                    finally:
                        self._threadsafe_submissions.pop(completion, None)

            if close_coroutine:
                coroutine.close()
            if error is not None:
                try:
                    completion.set_exception(error)
                except concurrent.futures.InvalidStateError:
                    pass
                loop.call_exception_handler(
                    {
                        "message": "MoviePilot 跨线程后台任务提交失败",
                        "exception": error,
                        "owner": owner,
                    }
                )
                return
            if task is None:
                return
            task.add_done_callback(mirror_completion)
            if completion.cancelled() and not task.done():
                task.cancel()

        def cancel_registered_task(
            submitted: concurrent.futures.Future[Any],
        ) -> None:
            """调用方取消 completion 时，把取消请求转交目标循环中的真实任务。"""
            if not submitted.cancelled():
                return
            close_coroutine = False
            with self._state_lock:
                task = submission.task
                if (
                    task is None
                    and not submission.coroutine_closed
                    and self._threadsafe_submissions.get(completion) is submission
                ):
                    self._threadsafe_submissions.pop(completion, None)
                    submission.coroutine_closed = True
                    close_coroutine = True
            if close_coroutine:
                coroutine.close()
            if task is not None and not task.done():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

        completion.add_done_callback(cancel_registered_task)
        with self._state_lock:
            if not self._accepting:
                coroutine.close()
                raise RuntimeError("后台任务登记器正在关闭，不能再创建新任务")
            self._threadsafe_submissions[completion] = submission
        try:
            loop.call_soon_threadsafe(submit_on_loop)
        except RuntimeError:
            with self._state_lock:
                if self._threadsafe_submissions.pop(completion, None) is submission:
                    submission.coroutine_closed = True
            if submission.coroutine_closed:
                coroutine.close()
            raise
        return completion

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
        self._shutdown_cancel_requested.discard(task)
        self._shutdown_timeout_reported.discard(task)
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

    async def shutdown(self, *, timeout_seconds: float = 10.0) -> bool:
        """停止接收并有限等待存量任务，返回全部 owner 是否真实收敛。"""
        with self._state_lock:
            self._accepting = False
            pending_submissions = tuple(self._threadsafe_submissions.values())
            self._threadsafe_submissions.clear()
            for submission in pending_submissions:
                submission.coroutine_closed = True
        for submission in pending_submissions:
            submission.coroutine.close()
            submission.completion.cancel()
        records = self.records
        tasks = [record.task for record in records]
        for record in records:
            if (
                record.cancel_on_shutdown
                and record.task not in self._shutdown_cancel_requested
            ):
                self._shutdown_cancel_requested.add(record.task)
                record.task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_seconds)

        unfinished = tuple(
            record
            for record in records
            if not record.task.done()
            and record.task not in self._shutdown_timeout_reported
        )
        if unfinished:
            self._shutdown_timeout_reported.update(
                record.task for record in unfinished
            )
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "MoviePilot 后台任务未在关停预算内结束",
                    "owners": tuple(record.owner for record in unfinished),
                    "tasks": tuple(record.task for record in unfinished),
                    "timeout_seconds": timeout_seconds,
                }
            )
        return all(record.task.done() for record in records)


_default_registry = TaskRegistry()
_runtime_registry: TaskRegistry | None = None


def configure_task_registry(registry: TaskRegistry | None) -> None:
    """由启动组合根发布当前 lifespan 的任务登记器。"""
    global _runtime_registry
    _runtime_registry = registry


def get_task_registry() -> TaskRegistry:
    """返回当前宿主任务登记器，未启动完整 lifespan 时保留测试兼容回退。"""
    return _runtime_registry or _default_registry
