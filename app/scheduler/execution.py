"""调度任务准入、执行、进度和事件循环桥接。"""

import asyncio
import inspect
import multiprocessing
import threading
import time
import traceback
from typing import Any, Callable, Optional

from app.application.messaging.message import MessageHelper
from app.application.scheduling import (  # noqa: E402
    JobExecutionState,
)
from app.runtime.correlation import call_with_correlation, get_correlation_id
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.observability import record_metric
from app.runtime.progress import ProgressHelper
from app.runtime.scheduling import TimerUtils
from app.scheduler.contract import _SchedulerOwnerBase
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.types import EventType

_message_helper_factory: Callable[[], MessageHelper] = MessageHelper


class SchedulerExecutionOwner(_SchedulerOwnerBase):
    """调度任务准入与函数执行语义。"""

    def _accepting_submissions(self) -> bool:
        """判断调度器是否仍允许提交新的运行实例。"""
        return self._lifecycle_state in {"starting", "running"}

    def _next_job_generation(self, job_id: str) -> int:
        """为同一 job 的下一次注册分配单调 generation。"""
        return self._registry.next_generation(job_id)

    def _assign_job_generation(self, job_id: str, job: dict[str, Any]) -> None:
        """把注册 generation 写入可变运行时状态。"""
        self._registry.assign_generation(job_id, job)

    def _is_job_active(self, job_id: str) -> bool:
        """判断任一 generation 的同 ID 任务是否仍在真实执行。"""
        return self._registry.is_active(job_id)

    def _release_job_generation(self, job_id: str, generation: int) -> None:
        """在任务真实收尾后释放对应 generation 的运行所有权。"""
        self._registry.release_generation(job_id, generation)

    def _prepare_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """
        准备定时任务
        """
        started_at = self._format_time()
        with self._lock:
            if not self._accepting_submissions():
                return None
            if not self._registry.consume_reservation(
                job_id,
                threading.get_ident(),
            ):
                return None
            job = self._jobs.get(job_id)
            if not job:
                return None
            if self._is_job_active(job_id):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                record_metric(
                    "scheduler.job.overlap_skip",
                    owner=str(job.get("owner", "unknown")),
                )
                return None
            if not JobExecutionState.begin(job, started_at):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                record_metric(
                    "scheduler.job.overlap_skip",
                    owner=str(job.get("owner", "unknown")),
                )
                return None
            generation = job.get("_generation", 0)
            if not self._registry.claim_generation(job_id, generation):
                JobExecutionState.finish(job, started_at, None)
                return None
            job["_metric_started_at"] = time.perf_counter()
        progress = ProgressHelper(self._get_progress_key(job_id))
        progress.start()
        progress.update(
            value=0,
            text=f"{job.get('name') or job_id} 开始执行 ...",
            data={
                "id": job_id,
                "_generation": job.get("_generation", 0),
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
            },
        )
        return job

    @staticmethod
    def _handle_job_error(job_id: str, job: dict[str, Any], error: Exception) -> None:
        """
        记录定时任务执行异常并发送系统错误事件。
        """
        logger.error(f"定时任务 {job.get('name')} 执行失败：{str(error)} - {traceback.format_exc()}")
        _message_helper_factory().put(
            title=f"{job.get('name')} 执行失败",
            message=str(error),
            role="system",
        )
        eventmanager.send_event(
            EventType.SystemError,
            {
                "type": "scheduler",
                "scheduler_id": job_id,
                "scheduler_name": job.get("name"),
                "error": str(error),
                "traceback": traceback.format_exc(),
            },
        )

    @staticmethod
    def _supports_progress_callback(func: Callable[..., Any]) -> bool:
        """
        判断定时任务函数是否显式支持进度回调参数。
        """
        try:
            parameters = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        return "progress_callback" in parameters

    @staticmethod
    def _get_result_error(result: Any) -> Optional[str]:
        """
        从定时任务标准失败返回值中提取错误信息。
        """
        if isinstance(result, tuple) and result and isinstance(result[0], bool) and result[0] is False:
            return str(result[1]) if len(result) > 1 and result[1] else "定时任务返回失败"
        return None

    async def _run_coro_job(
        self,
        coro_factory: Callable[[], Any],
        job_id: str,
        job: dict[str, Any],
        generation: Optional[int] = None,
    ) -> None:
        """
        在当前事件循环内执行协程定时任务并在真实完成后收敛状态。
        """
        generation = job.get("_generation", 0) if generation is None else generation
        success = True
        error = None
        try:
            result = await JobExecutionState.await_result(
                coro_factory(),
                timeout_seconds=job.get("timeout_seconds"),
            )
            error = self._get_result_error(result)
            success = error is None
        except asyncio.TimeoutError as err:
            success = False
            error = f"任务执行超时（{job.get('timeout_seconds')} 秒）"
            self._handle_job_error(job_id=job_id, job=job, error=err)
        except asyncio.CancelledError:
            success = False
            error = "任务已取消"
            raise
        except Exception as err:
            success = False
            error = str(err)
            self._handle_job_error(job_id=job_id, job=job, error=err)
        finally:
            # 协程收尾在事件循环上完成，同步路径（线程池/调用线程）提交到事件循环执行
            await self._finish_job(
                job_id=job_id,
                job=job,
                generation=generation,
                success=success,
                error=error,
            )

    def start(self, job_id: str, *args: Any, **kwargs: Any) -> bool:
        """
        启动定时服务
        """

        def __start_coro(
            coro_factory: Callable[[], Any],
            generation: int,
            runtime_job: dict[str, Any],
        ) -> tuple[bool, bool]:
            """
            启动协程，返回是否异步收尾以及本次提交是否被接受。
            """
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            target_loop = main_loop_registry.current
            target_loop_available = target_loop is not None and target_loop.is_running() and not target_loop.is_closed()
            if running_loop and (not target_loop_available or running_loop is target_loop):
                started = threading.Event()

                async def run_owned_job() -> None:
                    started.set()
                    await self._run_coro_job(
                        coro_factory=coro_factory,
                        job_id=job_id,
                        job=runtime_job,
                        generation=generation,
                    )

                with self._lock:
                    if not self._accepts_handle(job_id, generation):
                        return False, False
                    handle = running_loop.create_task(run_owned_job())
                    registered = self._register_handle(
                        job_id=job_id,
                        generation=generation,
                        loop=running_loop,
                        handle=handle,
                    )

                    def _finish_cancelled_before_start(
                        submitted: asyncio.Future[Any],
                    ) -> None:
                        if submitted.cancelled() and not started.is_set():
                            self._finish_unsubmitted_job(
                                job_id=job_id,
                                job=runtime_job,
                                generation=generation,
                                error="任务未提交",
                            )

                    handle.add_done_callback(_finish_cancelled_before_start)
                    return registered, registered
            if target_loop_available:
                wrapped = self._run_coro_job(
                    coro_factory=coro_factory,
                    job_id=job_id,
                    job=runtime_job,
                    generation=generation,
                )
                submitted = self._submit_cross_thread(
                    wrapped,
                    target_loop=target_loop,
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=lambda: self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=runtime_job,
                        generation=generation,
                        error="任务未提交",
                    ),
                )
                return submitted, submitted
            if self._lifecycle_state in {"stopping", "stopped"}:
                return False, False
            asyncio.run(coro_factory())
            return False, True

        # 获取定时任务
        job = self._prepare_job(job_id)
        if not job:
            return False
        generation = job.get("_generation", 0)
        success = True
        error = None
        deferred_finish = False
        accepted = True
        # 开始运行
        try:
            if not kwargs:
                kwargs = dict(job.get("kwargs") or {})
            func = job.get("func")
            if not func:
                return False
            if func == self.execute_agent_task:
                kwargs.setdefault("scheduler_generation", generation)
            if self._supports_progress_callback(func) and "progress_callback" not in kwargs:
                kwargs["progress_callback"] = self._build_progress_callback(job_id=job_id, job=job)
            # 是否多进程运行
            run_in_process = job.get("run_in_process", False)
            if inspect.iscoroutinefunction(func):
                # 协程函数
                deferred_finish, accepted = __start_coro(
                    lambda: func(*args, **kwargs),
                    generation,
                    job,
                )
            elif run_in_process:
                # 多进程运行
                p = multiprocessing.Process(
                    target=call_with_correlation,
                    args=(get_correlation_id(), func, args, kwargs),
                )
                p.start()
                p.join()
            else:
                # 普通函数
                result = func(*args, **kwargs)
                error = self._get_result_error(result)
                success = error is None
        except Exception as e:
            success = False
            error = str(e)
            self._handle_job_error(job_id=job_id, job=job, error=e)
        finally:
            if not deferred_finish:

                def finish_without_loop() -> None:
                    self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        error=error if accepted else "任务未提交",
                    )

                # 同步上下文执行异步收尾：优先提交到当前/全局事件循环，无循环时新建循环
                finish_submitted = self._submit_to_loop(
                    self._finish_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        success=success,
                        error=error,
                    ),
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=finish_without_loop,
                )
                if not finish_submitted:
                    finish_without_loop()
        return accepted

    def list(self) -> list[_SchemaScheduleInfo]:
        """
        当前所有任务
        """
        if not self._scheduler:
            return []
        with self._lock:
            # 返回计时任务
            schedulers = []
            # 去重
            added = []
            # 避免_scheduler.shutdown()处于阻塞状态导致的死锁
            if not self._scheduler or not self._scheduler.running:
                return []
            jobs = self._scheduler.get_jobs()
            # 按照下次运行时间排序
            jobs.sort(key=lambda x: x.next_run_time)
            # 将正在运行的任务提取出来 (保障一次性任务正常显示)
            for job_id, service in self._jobs.items():
                name = service.get("name")
                provider_name = service.get("provider_name")
                if (self._is_job_active(job_id) or service.get("running")) and name and provider_name:
                    if job_id not in added:
                        added.append(job_id)
                    progress = self.get_progress(job_id)
                    schedulers.append(
                        _SchemaScheduleInfo(
                            id=job_id,
                            name=name,
                            provider=provider_name,
                            status="正在运行",
                            progress=progress.value if progress else 0,
                            progress_text=progress.text if progress else None,
                            progress_enable=progress.enable if progress else False,
                            progress_detail=progress,
                        )
                    )
            # 获取其他待执行任务
            for job in jobs:
                job_id = job.id.split("|")[0]
                if job_id not in added:
                    added.append(job_id)
                else:
                    continue
                service_state = self._jobs.get(job_id)
                if not service_state:
                    continue
                # 任务状态
                status = "正在运行" if self._is_job_active(job_id) or service_state.get("running") else "等待"
                # 下次运行时间
                next_run = TimerUtils.time_difference(job.next_run_time)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=job.name,
                        provider=service_state.get("provider_name", "[系统]"),
                        status=status,
                        next_run=next_run,
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            # 仅手动执行的任务（未注册到调度器）
            for job_id, service in self._jobs.items():
                if not service.get("manual"):
                    continue
                if job_id in added:
                    continue
                added.append(job_id)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=service.get("name"),
                        provider=service.get("provider_name", "[系统]"),
                        status=("正在运行" if self._is_job_active(job_id) else "等待"),
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            return schedulers
