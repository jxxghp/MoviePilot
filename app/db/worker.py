"""同步数据库短事务的异步执行器。"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Callable, TypeVar

from app.runtime.observability import record_metric


T = TypeVar("T")


class DatabaseWorkerClosedError(RuntimeError):
    """数据库执行器尚未启动或已经停止。"""


class DatabaseWorkerOverloadedError(RuntimeError):
    """数据库执行器的运行与排队容量已经用尽。"""


@dataclass(frozen=True, slots=True)
class DatabaseWorkerStats:
    """数据库执行器当前的容量和任务数量。"""

    max_workers: int
    capacity: int
    queued: int
    running: int
    rejected: int
    closing: bool


@dataclass(slots=True)
class _WorkItem:
    """记录一个任务的排队时间和执行状态。"""

    submitted_at: float
    started_at: float | None = None


class DatabaseWorker:
    """以有限线程和队列执行不能原生异步化的数据库短事务。"""

    def __init__(self, *, max_workers: int = 4, capacity: int = 32) -> None:
        """保存容量配置，线程只在显式启动后创建。"""
        if max_workers < 1:
            raise ValueError("数据库 worker 线程数必须大于 0")
        if capacity < max_workers:
            raise ValueError("数据库 worker 总容量不能小于线程数")
        self._max_workers = max_workers
        self._capacity = capacity
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[
            Future[object], tuple[asyncio.Future[object], _WorkItem]
        ] = {}
        self._queued = 0
        self._running = 0
        self._rejected = 0
        self._closing = False

    async def start(self) -> None:
        """绑定当前事件循环并准备专属线程池。"""
        if self._executor is not None:
            if self._loop is not asyncio.get_running_loop():
                raise RuntimeError("数据库 worker 不能跨事件循环复用")
            return
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="moviepilot-db",
        )
        self._closing = False
        self._record_depth()

    def snapshot(self) -> DatabaseWorkerStats:
        """返回无需访问任务对象的低基数运行快照。"""
        with self._state_lock:
            return DatabaseWorkerStats(
                max_workers=self._max_workers,
                capacity=self._capacity,
                queued=self._queued,
                running=self._running,
                rejected=self._rejected,
                closing=self._closing,
            )

    async def run(self, operation: Callable[[], T]) -> T:
        """执行短事务，取消时仍等待已开始的事务取得最终结果。"""
        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None or self._loop is not loop or self._closing:
            raise DatabaseWorkerClosedError("数据库 worker 当前不可接收任务")

        with self._state_lock:
            if self._queued + self._running >= self._capacity:
                self._rejected += 1
                record_metric("db.worker.rejected")
                raise DatabaseWorkerOverloadedError(
                    f"数据库 worker 容量已用尽（上限 {self._capacity}）"
                )
            self._queued += 1

        item = _WorkItem(submitted_at=time.perf_counter())
        context = copy_context()
        try:
            future = executor.submit(self._execute, item, context.run, operation)
        except BaseException:
            with self._state_lock:
                self._queued -= 1
            self._record_depth()
            raise

        wrapped = asyncio.wrap_future(future, loop=loop)
        with self._state_lock:
            self._futures[future] = (wrapped, item)
        future.add_done_callback(
            lambda completed: self._schedule_completion(completed, item)
        )
        self._record_depth()

        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if not future.cancel():
                await self._wait_until_done(wrapped)
            if wrapped.done() and not wrapped.cancelled():
                wrapped.exception()
            raise

    def _execute(
        self,
        item: _WorkItem,
        context_run: Callable[..., T],
        operation: Callable[[], T],
    ) -> T:
        """在线程中标记任务开始并保留提交时的上下文。"""
        item.started_at = time.perf_counter()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._mark_running, item)
            except RuntimeError:
                pass
        return context_run(operation)

    def _mark_running(self, item: _WorkItem) -> None:
        """把任务从排队状态移入运行状态。"""
        with self._state_lock:
            self._queued -= 1
            self._running += 1
        started_at = item.started_at or time.perf_counter()
        record_metric("db.worker.wait", started_at - item.submitted_at)
        self._record_depth()

    def _schedule_completion(
        self,
        future: Future[object],
        item: _WorkItem,
    ) -> None:
        """把线程完成通知安全地回投到所属事件循环。"""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._complete, future, item)
        except RuntimeError:
            pass

    def _complete(self, future: Future[object], item: _WorkItem) -> None:
        """释放 admission，并记录任务的最终结果。"""
        with self._state_lock:
            self._futures.pop(future, None)
            if future.running() or future.done() and not future.cancelled():
                self._running -= 1
            else:
                self._queued -= 1
        outcome = "cancelled" if future.cancelled() else "success"
        if not future.cancelled():
            try:
                future.result()
            except BaseException:
                outcome = "error"
        started_at = item.started_at or item.submitted_at
        record_metric(
            "db.worker.duration",
            time.perf_counter() - started_at,
            outcome=outcome,
        )
        self._record_depth()

    async def _wait_until_done(self, future: asyncio.Future[object]) -> None:
        """忽略后续取消请求，直到线程内事务结束。"""
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break

    async def shutdown(self) -> None:
        """拒绝新任务，取消排队任务并等待运行中事务结束。"""
        executor = self._executor
        if executor is None:
            return
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("数据库 worker 必须在所属事件循环中停止")
        self._closing = True
        with self._state_lock:
            futures = tuple(self._futures.items())
        for future, _state in futures:
            future.cancel()
        for future, (wrapped, _item) in futures:
            if not future.cancelled():
                await self._wait_until_done(wrapped)
        executor.shutdown(wait=True, cancel_futures=True)
        while self.snapshot().queued or self.snapshot().running:
            await asyncio.sleep(0)
        self._executor = None
        self._record_depth()

    def _record_depth(self) -> None:
        """记录当前排队量与运行量。"""
        stats = self.snapshot()
        record_metric("db.worker.queue.depth", stats.queued)
        record_metric("db.worker.active", stats.running)
