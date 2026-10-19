"""
同步数据库短事务的异步执行器。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar, cast

from app.runtime.observability import record_metric
from app.runtime.priority import (
    INTERACTIVE_DATABASE_CAPACITY,
    INTERACTIVE_DATABASE_WORKERS,
    is_interactive,
)
from app.schemas.exception import (
    DatabaseWorkerClosedError,
    DatabaseWorkerOverloadedError,
)

T = TypeVar("T")

DATABASE_WORKER_MAX_WORKERS = 4
DATABASE_WORKER_CAPACITY = 32


@dataclass(frozen=True, slots=True)
class DatabaseWorkerStats:
    """
    单个数据库执行组当前的容量和任务数量快照。

    :param max_workers: 最大工作线程数
    :param capacity: 包含排队和运行中任务的总容量上限
    :param queued: 当前等待线程执行的任务数
    :param running: 当前正在工作线程内执行的任务数
    :param rejected: 执行器累计因过载拒绝的任务数
    :param closing: 是否已经开始关闭，不再接受新任务
    """

    max_workers: int
    capacity: int
    queued: int
    running: int
    rejected: int
    closing: bool


@dataclass(slots=True)
class _WorkItem:
    """
    记录一个数据库短事务的排队和开始执行时间。

    :param submitted_at: 任务提交时的单调时钟读数，单位为秒
    :param started_at: 工作线程开始执行的单调时钟读数；None 表示尚未开始
    """

    submitted_at: float
    started_at: float | None = None


class DatabaseWorker:
    """
    以有限线程和队列执行不能原生异步化的数据库短事务。
    """

    def __init__(
        self,
        *,
        max_workers: int = DATABASE_WORKER_MAX_WORKERS,
        capacity: int = DATABASE_WORKER_CAPACITY,
        reserve_interactive: bool = True,
    ) -> None:
        """
        保存容量配置，线程只在显式启动后创建。

        :param max_workers: 普通数据库事务的工作线程上限
        :param capacity: 普通事务的运行和排队总容量
        :param reserve_interactive: 是否额外保留独立的交互短事务 worker 和队列
        """
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
        self._reported_queued = 0
        self._reported_running = 0
        self._rejected = 0
        self._closing = False
        self._interactive_worker = (
            DatabaseWorker(
                max_workers=INTERACTIVE_DATABASE_WORKERS,
                capacity=INTERACTIVE_DATABASE_CAPACITY,
                reserve_interactive=False,
            ) if reserve_interactive else None
        )

    async def start(self) -> None:
        """
        绑定当前事件循环并准备专属线程池。
        """
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
        if self._interactive_worker is not None:
            await self._interactive_worker.start()
        self._record_depth()

    def snapshot(self) -> DatabaseWorkerStats:
        """
        返回普通执行组的低基数快照；交互保留组通过 interactive_snapshot 单独读取。
        """
        with self._state_lock:
            return DatabaseWorkerStats(
                max_workers=self._max_workers,
                capacity=self._capacity,
                queued=self._queued,
                running=self._running,
                rejected=self._rejected,
                closing=self._closing,
            )

    def interactive_snapshot(self) -> Optional[DatabaseWorkerStats]:
        """
        返回交互保留组的容量和负载；未配置保留组时返回 None。
        """
        return self._interactive_worker.snapshot() if self._interactive_worker is not None else None

    async def run(self, operation: Callable[[], T]) -> T:
        """
        执行短事务；结果发布和取消返回均晚于容量回收，取消仍等待已开始事务收口。
        """
        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None or self._loop is not loop or self._closing:
            raise DatabaseWorkerClosedError("数据库 worker 当前不可接收任务")
        if self._interactive_worker is not None and is_interactive():
            return await self._interactive_worker.run(operation)

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

        # 不能直接包装原始 Future：其结果转发可能先于完成记账，晚注册回调还会立即执行。
        wrapped: asyncio.Future[object] = loop.create_future()
        with self._state_lock:
            self._futures[future] = (wrapped, item)
        future.add_done_callback(
            lambda completed: self._schedule_completion(completed, item)
        )
        self._record_depth()

        try:
            return cast(T, await asyncio.shield(wrapped))
        except asyncio.CancelledError:
            future.cancel()
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
        """
        在线程中标记任务开始并保留提交时的上下文。
        """
        item.started_at = time.perf_counter()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._mark_running, item)
            except RuntimeError:
                pass
        return context_run(operation)

    def _mark_running(self, item: _WorkItem) -> None:
        """
        把任务从排队状态移入运行状态。
        """
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
        """
        把线程完成通知安全地回投到所属事件循环。
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._complete, future, item)
        except RuntimeError:
            pass

    def _complete(self, future: Future[object], item: _WorkItem) -> None:
        """
        先释放 admission，再发布最终结果；同一事件循环回调保证调用方观察顺序。
        """
        with self._state_lock:
            wrapped, _ = self._futures.pop(future)
            if future.running() or future.done() and not future.cancelled():
                self._running -= 1
            else:
                self._queued -= 1
        outcome = self._publish_result(future, wrapped)
        started_at = item.started_at or item.submitted_at
        record_metric(
            "db.worker.duration",
            time.perf_counter() - started_at,
            outcome=outcome,
        )
        self._record_depth()

    @staticmethod
    def _publish_result(future: Future[object], wrapped: asyncio.Future[object]) -> str:
        """
        保留原 wrap_future 的取消和异常翻译合同，不调用 asyncio 私有实现。
        """
        if future.cancelled():
            wrapped.cancel()
            return "cancelled"
        try:
            result = future.result()
        except BaseException as error:
            exception_types: dict[type[BaseException], type[BaseException]] = {
                concurrent.futures.CancelledError: asyncio.CancelledError,
                concurrent.futures.InvalidStateError: asyncio.InvalidStateError,
            }
            exception_type = exception_types.get(type(error))
            if exception_type is not None:
                error = exception_type(*error.args).with_traceback(error.__traceback__)
            wrapped.set_exception(error)
            return "error"
        wrapped.set_result(result)
        return "success"

    async def _wait_until_done(
        self,
        future: asyncio.Future[object],
        *,
        interruptible: bool = False,
    ) -> None:
        """
        等待线程内事务结束，并按调用场景决定是否响应外层取消。
        """
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                if interruptible:
                    raise
                continue
            except BaseException:
                break

    async def shutdown(self) -> None:
        """
        拒绝新任务，取消排队任务并等待运行中事务结束。
        """
        executor = self._executor
        if executor is None:
            return
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("数据库 worker 必须在所属事件循环中停止")
        self._closing = True
        if self._interactive_worker is not None:
            await self._interactive_worker.shutdown()
        with self._state_lock:
            futures = tuple(self._futures.items())
        for future, _state in futures:
            future.cancel()
        for future, (wrapped, _item) in futures:
            if not future.cancelled():
                # 关停超时必须能返回并保留 owner；已开始的数据库事务继续由线程完成。
                await self._wait_until_done(wrapped, interruptible=True)
        executor.shutdown(wait=True, cancel_futures=True)
        while self.snapshot().queued or self.snapshot().running:
            await asyncio.sleep(0)
        self._executor = None
        self._record_depth()

    def _record_depth(self) -> None:
        """
        以状态变化量记录队列和运行中的任务数量。
        """
        stats = self.snapshot()
        queued_delta = stats.queued - self._reported_queued
        running_delta = stats.running - self._reported_running
        if queued_delta:
            record_metric("db.worker.queue.depth", queued_delta)
        if running_delta:
            record_metric("db.worker.active", running_delta)
        self._reported_queued = stats.queued
        self._reported_running = stats.running
