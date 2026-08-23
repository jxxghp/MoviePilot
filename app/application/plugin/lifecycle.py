"""插件安装与启动同步之间共享的生命周期互斥。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager


class PluginLifecycleCoordinator:
    """在事件循环和同步启动线程之间协调插件生命周期操作。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_plugins: set[str] = set()
        self._startup_active = False

    @staticmethod
    def _normalize(plugin_id: str) -> str:
        return (plugin_id or "").strip().lower()

    def _try_acquire_plugin(self, plugin_id: str) -> bool:
        normalized_id = self._normalize(plugin_id)
        if not normalized_id:
            raise ValueError("插件ID不能为空")
        with self._condition:
            if self._startup_active or normalized_id in self._active_plugins:
                return False
            self._active_plugins.add(normalized_id)
            return True

    def _release_plugin(self, plugin_id: str) -> None:
        normalized_id = self._normalize(plugin_id)
        with self._condition:
            self._active_plugins.discard(normalized_id)
            self._condition.notify_all()

    def _try_acquire_startup(self) -> bool:
        with self._condition:
            if self._startup_active or self._active_plugins:
                return False
            self._startup_active = True
            return True

    def _release_startup(self) -> None:
        with self._condition:
            self._startup_active = False
            self._condition.notify_all()

    @asynccontextmanager
    async def hold(self, plugin_id: str):
        """异步持有单个插件的生命周期资格，不在线程池中等待锁。"""
        while not self._try_acquire_plugin(plugin_id):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._release_plugin(plugin_id)

    @asynccontextmanager
    async def hold_startup(self):
        """异步持有启动同步的全局资格，阻止安装请求穿过启动收口。"""
        while not self._try_acquire_startup():
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._release_startup()


plugin_lifecycle = PluginLifecycleCoordinator()
