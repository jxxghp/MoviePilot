"""进程停止与细粒度取消信号契约。"""

from __future__ import annotations

import threading
from typing import Protocol


class StopState(Protocol):
    """向运行时消费者暴露系统、工作流和整理任务的停止状态。"""

    @property
    def is_system_stopped(self) -> bool:
        """返回系统是否已经进入停止阶段。"""
        ...

    def stop_system(self) -> None:
        """发布不可逆的当前进程停止信号。"""
        ...

    def stop_workflow(self, workflow_id: int) -> None:
        """请求停止指定工作流。"""
        ...

    def resume_workflow(self, workflow_id: int) -> None:
        """清除指定工作流的停止请求。"""
        ...

    def is_workflow_stopped(self, workflow_id: int) -> bool:
        """返回系统或指定工作流是否已经停止。"""
        ...

    def stop_transfer(self, path: str) -> None:
        """登记指定源路径的一次性整理停止请求。"""
        ...

    def consume_transfer_stop(self, path: str) -> bool:
        """消费指定路径的停止请求；系统停止时始终返回真。"""
        ...


class ProcessStopState:
    """线程安全地持有当前进程的停止与细粒度取消状态。"""

    def __init__(self) -> None:
        self._system_event = threading.Event()
        self._workflow_ids: set[int] = set()
        self._transfer_paths: set[str] = set()
        self._lock = threading.Lock()

    @property
    def system_event(self) -> threading.Event:
        """返回兼容入口使用的系统停止事件。"""
        return self._system_event

    def replace_system_event(self, event: threading.Event) -> None:
        """替换系统事件，仅供旧 ABI 与隔离测试继续使用。"""
        self._system_event = event

    @property
    def is_system_stopped(self) -> bool:
        """返回系统是否已经进入停止阶段。"""
        return self._system_event.is_set()

    def stop_system(self) -> None:
        """发布不可逆的当前进程停止信号。"""
        self._system_event.set()

    def stop_workflow(self, workflow_id: int) -> None:
        """请求停止指定工作流。"""
        with self._lock:
            self._workflow_ids.add(workflow_id)

    def resume_workflow(self, workflow_id: int) -> None:
        """清除指定工作流的停止请求。"""
        with self._lock:
            self._workflow_ids.discard(workflow_id)

    def is_workflow_stopped(self, workflow_id: int) -> bool:
        """返回系统或指定工作流是否已经停止。"""
        if self.is_system_stopped:
            return True
        with self._lock:
            return workflow_id in self._workflow_ids

    def stop_transfer(self, path: str) -> None:
        """登记指定源路径的一次性整理停止请求。"""
        with self._lock:
            self._transfer_paths.add(path)

    def consume_transfer_stop(self, path: str) -> bool:
        """消费指定路径的停止请求；系统停止时始终返回真。"""
        if self.is_system_stopped:
            return True
        with self._lock:
            if path not in self._transfer_paths:
                return False
            self._transfer_paths.remove(path)
            return True

    def workflow_stop_ids(self) -> list[int]:
        """返回旧诊断入口需要的工作流停止快照。"""
        with self._lock:
            return list(self._workflow_ids)

    def transfer_stop_paths(self) -> list[str]:
        """返回旧诊断入口需要的整理停止快照。"""
        with self._lock:
            return list(self._transfer_paths)


runtime_stop_state = ProcessStopState()
