"""Scheduler 执行 generation、预约与异步句柄的唯一状态 owner。"""

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass
from typing import Any, Optional, Union

_FutureHandle = Union[
    asyncio.Future[Any],
    concurrent.futures.Future[Any],
]


@dataclass(slots=True)
class SchedulerHandle:
    """记录 Scheduler 投递的执行句柄及其真实完成信号。"""

    job_id: str
    generation: int
    loop: asyncio.AbstractEventLoop
    handle: _FutureHandle
    completion: _FutureHandle
    kind: str


class ExecutionRegistry:
    """原子维护任务 generation、运行所有权、预约和异步句柄。"""

    def __init__(self, lock: Optional[Any] = None) -> None:
        """创建空 registry；可复用 Facade 的可重入锁形成统一临界区。"""
        self._lock = lock if lock is not None else threading.RLock()
        self._handles: dict[int, SchedulerHandle] = {}
        self._generations: dict[str, int] = {}
        self._active_generations: dict[str, set[int]] = {}
        self._reservations: dict[str, int] = {}

    def next_generation(self, job_id: str) -> int:
        """为指定任务原子分配单调递增的 generation。"""
        with self._lock:
            generation = self._generations.get(job_id, 0) + 1
            self._generations[job_id] = generation
            return generation

    def assign_generation(self, job_id: str, job: dict[str, Any]) -> int:
        """分配 generation 并写入兼容 Facade 使用的任务状态。"""
        with self._lock:
            generation = self._generations.get(job_id, 0) + 1
            self._generations[job_id] = generation
            job["_generation"] = generation
            return generation

    def current_generation(self, job_id: str) -> int:
        """返回任务最近分配的 generation，尚未分配时返回零。"""
        with self._lock:
            return self._generations.get(job_id, 0)

    def claim_generation(self, job_id: str, generation: int) -> bool:
        """在同 ID 无活跃任务时取得 generation 的唯一运行所有权。"""
        with self._lock:
            active = self._active_generations.get(job_id)
            if active:
                return False
            self._active_generations[job_id] = {generation}
            return True

    def is_active(self, job_id: str) -> bool:
        """判断任一 generation 的同 ID 任务是否仍在执行。"""
        with self._lock:
            return bool(self._active_generations.get(job_id))

    def active_generations(self, job_id: str) -> frozenset[int]:
        """返回指定任务当前活跃 generation 的不可变快照。"""
        with self._lock:
            return frozenset(self._active_generations.get(job_id, set()))

    def release_generation(self, job_id: str, generation: int) -> bool:
        """释放匹配的运行所有权，返回本次是否实际移除 generation。"""
        with self._lock:
            active = self._active_generations.get(job_id)
            if not active or generation not in active:
                return False
            active.remove(generation)
            if not active:
                self._active_generations.pop(job_id, None)
            return True

    def reserve(self, job_id: str, owner: int) -> bool:
        """为手动触发原子预约任务；已活跃或已预约时拒绝。"""
        with self._lock:
            if self._active_generations.get(job_id) or job_id in self._reservations:
                return False
            self._reservations[job_id] = owner
            return True

    def reservation_owner(self, job_id: str) -> Optional[int]:
        """返回当前预约线程标识，无预约时返回 None。"""
        with self._lock:
            return self._reservations.get(job_id)

    def consume_reservation(self, job_id: str, owner: int) -> bool:
        """校验并消费调用方预约；无预约时允许普通调度继续。"""
        with self._lock:
            reserved_owner = self._reservations.get(job_id)
            if reserved_owner is None:
                return True
            if reserved_owner != owner:
                return False
            self._reservations.pop(job_id, None)
            return True

    def release_reservation(self, job_id: str, owner: Optional[int] = None) -> bool:
        """释放预约；指定 owner 时不得清除其他调用方的预约。"""
        with self._lock:
            reserved_owner = self._reservations.get(job_id)
            if reserved_owner is None or (owner is not None and reserved_owner != owner):
                return False
            self._reservations.pop(job_id, None)
            return True

    def register_handle(
        self,
        *,
        job_id: str,
        generation: int,
        loop: asyncio.AbstractEventLoop,
        handle: _FutureHandle,
        completion: Optional[_FutureHandle] = None,
        kind: str = "job",
    ) -> SchedulerHandle:
        """按真实完成信号登记 Scheduler 拥有的异步句柄。"""
        if completion is None:
            completion = handle
        scheduler_handle = SchedulerHandle(
            job_id=job_id,
            generation=generation,
            loop=loop,
            handle=handle,
            completion=completion,
            kind=kind,
        )
        with self._lock:
            self._handles[id(completion)] = scheduler_handle
        return scheduler_handle

    def remove_handle(self, completion: _FutureHandle) -> bool:
        """按真实完成信号摘除句柄，返回是否存在对应登记。"""
        with self._lock:
            return self._handles.pop(id(completion), None) is not None

    def handles(
        self,
        *,
        job_id: Optional[str] = None,
        generation: Optional[int] = None,
        kind: Optional[str] = None,
    ) -> tuple[SchedulerHandle, ...]:
        """按任务、generation 与用途返回当前句柄的稳定快照。"""
        with self._lock:
            return tuple(
                handle
                for handle in self._handles.values()
                if (job_id is None or handle.job_id == job_id)
                and (generation is None or handle.generation == generation)
                and (kind is None or handle.kind == kind)
            )

    def stop_snapshot(self) -> tuple[SchedulerHandle, ...]:
        """清除尚未消费的预约并返回停止阶段需要收口的句柄快照。"""
        with self._lock:
            self._reservations.clear()
            return tuple(self._handles.values())

    def clear_reservations(self) -> None:
        """在热重载封闭提交入口时清除尚未消费的手动预约。"""
        with self._lock:
            self._reservations.clear()
