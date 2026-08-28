"""调度器事件循环提交与异步句柄桥接。"""

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable, Optional

from app.runtime.loop import main_loop_registry
from app.scheduler.contract import _SchedulerOwnerBase
from app.scheduler.registry import SchedulerHandle


class SchedulerBridgeOwner(_SchedulerOwnerBase):
    """调度器事件循环提交与异步句柄桥接。"""

    def _remove_handle(
        self,
        handle: asyncio.Future[Any] | concurrent.futures.Future[Any],
    ) -> None:
        """执行句柄完成后从 owner registry 移除。"""
        with self._lock:
            self._registry.remove_handle(handle)

    def _register_handle(
        self,
        job_id: str,
        generation: int,
        loop: asyncio.AbstractEventLoop,
        handle: asyncio.Future[Any] | concurrent.futures.Future[Any],
        completion: asyncio.Future[Any] | concurrent.futures.Future[Any] | None = None,
        kind: str = "job",
    ) -> bool:
        """登记调度器拥有的句柄；关闭竞态下拒绝并取消新句柄。"""
        if completion is None:
            completion = handle
        with self._lock:
            if not self._accepts_handle(job_id, generation):
                if isinstance(handle, concurrent.futures.Future):
                    handle.cancel()
                elif loop.is_running():
                    loop.call_soon_threadsafe(handle.cancel)
                else:
                    handle.cancel()
                return False
            self._registry.register_handle(
                job_id=job_id,
                generation=generation,
                loop=loop,
                handle=handle,
                completion=completion,
                kind=kind,
            )
        completion.add_done_callback(self._remove_handle)
        return True

    def _accepts_handle(self, job_id: str, generation: int) -> bool:
        """判断新句柄是否属于当前运行期或热重载中的既有任务。"""
        if self._accepting_submissions():
            return True
        current_job = self._jobs.get(job_id)
        return bool(
            self._lifecycle_state == "reloading"
            and current_job is not None
            and current_job.get("_generation", 0) == generation
            and current_job.get("running")
        )

    @staticmethod
    def _cancel_handle(handle: SchedulerHandle) -> None:
        """从句柄所属线程安全地请求取消。"""
        target = handle.handle
        if isinstance(target, concurrent.futures.Future):
            target.cancel()
            return
        if target.done():
            return
        if target.get_loop().is_running():
            target.get_loop().call_soon_threadsafe(target.cancel)
        else:
            target.cancel()

    @staticmethod
    async def _wait_handle(handle: SchedulerHandle) -> None:
        """等待取消请求到达协程 finally，而不是只等待提交代理变为 cancelled。"""
        target = handle.completion
        if isinstance(target, concurrent.futures.Future):
            await asyncio.shield(asyncio.wrap_future(target))
            return
        if target.get_loop() is asyncio.get_running_loop():
            await asyncio.shield(target)

    async def _await_cancelled_handles(
        self,
        handles: tuple[SchedulerHandle, ...],
    ) -> None:
        """等待已投递协程结束，关闭总预算由应用生命周期统一控制。"""
        if not handles:
            return
        await asyncio.gather(
            *(self._wait_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    async def _await_progress_handles(self, job_id: str, generation: int) -> None:
        """等待同一轮任务已提交的进度更新，保证最终状态最后写入缓存。"""
        with self._lock:
            handles = self._registry.handles(
                job_id=job_id,
                generation=generation,
                kind="progress",
            )
        if not handles:
            return
        await asyncio.gather(
            *(self._wait_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    @staticmethod
    def _track_cross_thread_completion(
        coro: Any,
        completion: concurrent.futures.Future[Any],
        started: threading.Event,
    ) -> Any:
        """把跨线程提交代理与协程真实终态分离。"""

        async def _tracked() -> None:
            started.set()
            try:
                result = await coro
            except asyncio.CancelledError:
                if not completion.done():
                    completion.cancel()
            except Exception as err:
                if not completion.done():
                    completion.set_exception(err)
            else:
                if not completion.done():
                    completion.set_result(result)

        return _tracked()

    def _submit_cross_thread(
        self,
        coro: Any,
        *,
        target_loop: asyncio.AbstractEventLoop,
        job_id: str,
        generation: int,
        on_unstarted_cancel: Optional[Callable[[], None]] = None,
        kind: str = "job",
    ) -> bool:
        """向主循环提交协程，并以独立完成信号跟踪真实收尾。"""
        completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
        handle: concurrent.futures.Future[Any] = concurrent.futures.Future()
        started = threading.Event()
        tracked = self._track_cross_thread_completion(coro, completion, started)
        task_lock = threading.Lock()
        target_task: asyncio.Task[Any] | None = None

        def complete_target_task(task: asyncio.Task[Any]) -> None:
            if task.cancelled() and not started.is_set():
                if on_unstarted_cancel:
                    on_unstarted_cancel()
                if not completion.done():
                    completion.cancel()
            elif not completion.done():
                error = task.exception()
                if error is None:
                    completion.set_result(None)
                else:
                    completion.set_exception(error)
            if not handle.done():
                handle.set_result(None)

        def start_on_target_loop() -> None:
            nonlocal target_task
            with task_lock:
                if handle.cancelled():
                    tracked.close()
                    coro.close()
                    if on_unstarted_cancel:
                        on_unstarted_cancel()
                    completion.cancel()
                    return
                target_task = target_loop.create_task(tracked)
                target_task.add_done_callback(complete_target_task)

        def cancel_target_task(submitted: concurrent.futures.Future[Any]) -> None:
            if not submitted.cancelled():
                return
            with task_lock:
                task = target_task
            if task is not None and not task.done():
                target_loop.call_soon_threadsafe(task.cancel)

        with self._lock:
            if not self._accepts_handle(job_id, generation):
                tracked.close()
                coro.close()
                return False
            try:
                target_loop.call_soon_threadsafe(start_on_target_loop)
            except RuntimeError:
                tracked.close()
                coro.close()
                return False

            registered = self._register_handle(
                job_id=job_id,
                generation=generation,
                loop=target_loop,
                handle=handle,
                completion=completion,
                kind=kind,
            )
            handle.add_done_callback(cancel_target_task)
        return registered

    def _submit_to_loop(
        self,
        coro: Any,
        *,
        job_id: str,
        generation: int = 0,
        on_unstarted_cancel: Optional[Callable[[], None]] = None,
        kind: str = "job",
    ) -> bool:
        """
        把协程提交到事件循环执行，兼容以下调用环境：
        - 应用主循环可用：统一由主循环拥有任务和关闭顺序
        - 仅调用方循环可用：在当前循环排队为独立任务
        - 无运行中循环（测试/CLI）：新建循环同步执行，确保进度不丢失

        job 标识是所有权键；所有句柄都由 Scheduler 持有，关闭时可以取消并等待。
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        target_loop = main_loop_registry.current
        target_loop_available = target_loop is not None and target_loop.is_running() and not target_loop.is_closed()
        if running_loop and (not target_loop_available or running_loop is target_loop):
            with self._lock:
                if not self._accepts_handle(job_id, generation):
                    coro.close()
                    return False
                handle = running_loop.create_task(coro)
                registered = self._register_handle(
                    job_id=job_id,
                    generation=generation,
                    loop=running_loop,
                    handle=handle,
                    kind=kind,
                )
                if on_unstarted_cancel:
                    handle.add_done_callback(lambda submitted: on_unstarted_cancel() if submitted.cancelled() else None)
                return registered
        elif target_loop is not None and target_loop_available:
            return self._submit_cross_thread(
                coro,
                target_loop=target_loop,
                job_id=job_id,
                generation=generation,
                on_unstarted_cancel=on_unstarted_cancel,
                kind=kind,
            )
        elif self._lifecycle_state in {"stopping", "stopped"}:
            coro.close()
            return False
        else:
            asyncio.run(coro)
            return True
