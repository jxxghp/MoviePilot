"""进程主事件循环的显式生命周期登记。"""

from __future__ import annotations

import threading
from asyncio import AbstractEventLoop


class MainLoopRegistry:
    """以 owner 身份管理可供跨线程投递的主事件循环。"""

    def __init__(self) -> None:
        """初始化空 owner 集合与互斥锁。"""
        self._current: AbstractEventLoop | None = None
        self._owners: dict[object, AbstractEventLoop] = {}
        self._lock = threading.Lock()

    @property
    def current(self) -> AbstractEventLoop | None:
        """返回当前登记值，不校验循环是否仍可运行。"""
        with self._lock:
            return self._current

    def require(self) -> AbstractEventLoop:
        """返回可运行主循环；未启动或已关闭时明确失败。"""
        loop = self.current
        if loop is None or not loop.is_running() or loop.is_closed():
            raise RuntimeError("主事件循环尚未启动或已经停止")
        return loop

    def register(self, loop: AbstractEventLoop) -> object:
        """登记一个生命周期 owner，并返回仅供对应关闭路径释放的身份。"""
        owner = object()
        with self._lock:
            self._owners[owner] = loop
            self._current = loop
        return owner

    def release(self, owner: object) -> None:
        """释放指定 owner，迟到的旧关闭不得清除更新的登记。"""
        with self._lock:
            if owner not in self._owners:
                return
            self._owners.pop(owner)
            self._current = next(reversed(self._owners.values()), None)

    def replace_compat(self, loop: AbstractEventLoop | None) -> None:
        """兼容旧属性赋值，仅替换当前投递目标而不伪造 owner。"""
        with self._lock:
            self._current = loop


main_loop_registry = MainLoopRegistry()
