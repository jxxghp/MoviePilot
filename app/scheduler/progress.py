"""调度任务进度快照与终态收敛。"""

import time
from datetime import datetime
from typing import Any, Callable, Optional

from app.application.scheduling import (  # noqa: E402
    JobExecutionState,
)
from app.runtime.observability import record_metric
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.scheduler.contract import _SchedulerOwnerBase
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress

SCHEDULER_PROGRESS_PREFIX = "scheduler"


class SchedulerProgressOwner(_SchedulerOwnerBase):
    """调度任务进度快照与终态收敛。"""

    def _finish_unsubmitted_job(
        self,
        job_id: str,
        job: dict[str, Any],
        generation: int,
        error: Optional[str],
    ) -> None:
        """收尾协程无法提交时同步释放任务状态和运行所有权。"""
        finished_at = self._format_time()
        metric_started_at = None
        with self._lock:
            if generation not in self._registry.active_generations(job_id):
                return
            current_job = self._jobs.get(job_id)
            if current_job is job and current_job.get("_generation", 0) == generation:
                JobExecutionState.finish(job, finished_at, error)
                metric_started_at = job.pop("_metric_started_at", None)
            self._release_job_generation(job_id, generation)
        if metric_started_at is not None:
            record_metric(
                "scheduler.job.duration",
                time.perf_counter() - metric_started_at,
                owner=str(job.get("owner", "unknown")),
                outcome="success" if error is None else "error",
            )

    @staticmethod
    def _get_progress_key(job_id: str) -> str:
        """
        获取定时服务进度缓存键。
        """
        return f"{SCHEDULER_PROGRESS_PREFIX}:{job_id}"

    @staticmethod
    def _format_time(value: Optional[datetime] = None) -> str:
        """
        格式化进度事件时间。
        """
        return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    async def _finish_job(
        self,
        job_id: str,
        job: dict[str, Any],
        generation: int,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """
        完成定时任务
        """
        # 业务函数返回前提交的进度回调可能仍在等待 Redis I/O；先收敛它们，
        # 避免迟到的 running 快照覆盖 success/failed 终态。
        await self._await_progress_handles(job_id, generation)
        finished_at = self._format_time()
        with self._lock:
            current_job = self._jobs.get(job_id)
            if current_job is not job or current_job.get("_generation", 0) != generation:
                self._release_job_generation(job_id, generation)
                return
            JobExecutionState.finish(job, finished_at, error)
            metric_started_at = job.pop("_metric_started_at", None)
            if metric_started_at is not None:
                record_metric(
                    "scheduler.job.duration",
                    time.perf_counter() - metric_started_at,
                    owner=str(job.get("owner", "unknown")),
                    outcome="success" if success else "error",
                )
        job_name = job.get("name") if job else job_id
        # 收尾可能发生在事件循环上（_run_coro_job），使用异步进度后端避免阻塞
        progress = AsyncProgressHelper(self._get_progress_key(job_id))
        try:
            current_progress = await progress.get() or {}
            progress_value = 100 if success else current_progress.get("value", 0)
            await progress.end(
                text=f"{job_name} {'执行完成' if success else '执行失败'}",
                data={
                    "id": job_id,
                    "_generation": generation,
                    "name": job_name,
                    "provider": job.get("provider_name", "[系统]") if job else None,
                    "status": "success" if success else "failed",
                    "success": success,
                    "finished_at": finished_at,
                    "error": error,
                },
                value=progress_value,
            )
        finally:
            with self._lock:
                self._release_job_generation(job_id, generation)

    def get_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度。
        """
        if not job_id:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            job_name = job.get("name") if job else job_id
            provider_name = job.get("provider_name", "[系统]") if job else None
            running = bool(job and (self._is_job_active(job_id) or job.get("running")))
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        detail = ProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = dict(detail.get("data") or {})
        progress_generation = data.pop("_generation", None)
        if job and progress_generation is not None and progress_generation != job.get("_generation", 0):
            detail = {}
            data = {}
        value = detail.get("value", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        return _SchemaScheduleProgress(
            id=job_id,
            name=data.get("name") or job_name,
            provider=data.get("provider") or provider_name,
            enable=bool(detail.get("enable", running)),
            value=max(min(value, 100), 0),
            text=detail.get("text"),
            status=data.get("status") or ("running" if running else "waiting"),
            success=data.get("success"),
            started_at=data.get("started_at") or last_started_at,
            finished_at=data.get("finished_at") or last_finished_at,
            error=data.get("error") or last_error,
            data=data,
        )

    async def aget_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度（异步版本，供事件循环上的端点使用）。
        """
        if not job_id:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            job_name = job.get("name") if job else job_id
            provider_name = job.get("provider_name", "[系统]") if job else None
            running = bool(job and (self._is_job_active(job_id) or job.get("running")))
            last_started_at = job.get("last_started_at") if job else None
            last_finished_at = job.get("last_finished_at") if job else None
            last_error = job.get("last_error") if job else None
        # 异步后端读取，避免在事件循环上阻塞
        detail = await AsyncProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        data = dict(detail.get("data") or {})
        progress_generation = data.pop("_generation", None)
        if job and progress_generation is not None and progress_generation != job.get("_generation", 0):
            detail = {}
            data = {}
        value = detail.get("value", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        return _SchemaScheduleProgress(
            id=job_id,
            name=data.get("name") or job_name,
            provider=data.get("provider") or provider_name,
            enable=bool(detail.get("enable", running)),
            value=max(min(value, 100), 0),
            text=detail.get("text"),
            status=data.get("status") or ("running" if running else "waiting"),
            success=data.get("success"),
            started_at=data.get("started_at") or last_started_at,
            finished_at=data.get("finished_at") or last_finished_at,
            error=data.get("error") or last_error,
            data=data,
        )

    def _build_progress_callback(self, job_id: str, job: dict[str, Any]) -> Callable[..., None]:
        """
        构建传递给定时任务内部的进度更新回调。
        """
        generation = job.get("_generation", 0)

        def update_progress(
            value: Optional[float] = None,
            text: Optional[str] = None,
            data: Optional[dict[str, Any]] = None,
        ) -> None:
            """
            更新当前定时任务进度。
            """
            progress_data = {
                "id": job_id,
                "_generation": generation,
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
            }
            if data:
                progress_data.update(data)
            key = self._get_progress_key(job_id)

            async def _update() -> None:
                with self._lock:
                    current_job = self._jobs.get(job_id)
                    if current_job is not job or current_job.get("_generation", 0) != generation:
                        return
                # 异步后端更新，避免任务函数在事件循环内调用回调时阻塞
                await AsyncProgressHelper(key).update(
                    value=value,
                    text=text,
                    data=progress_data,
                )

            # 回调可能在事件循环内（async 任务）或线程池中（sync 任务）被调用，
            # 统一经事件循环提交；无运行中循环时同步执行兜底
            self._submit_to_loop(
                _update(),
                job_id=job_id,
                generation=job.get("_generation", 0),
                kind="progress",
            )

        return update_progress
