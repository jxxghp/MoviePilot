"""插件安装与启动同步之间共享的生命周期互斥。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class PluginStartupLease:
    """启动 lease 的不透明能力句柄，仅按对象身份由所属协调器认可。"""

    __slots__ = ()


class PluginLifecycleCoordinator:
    """在事件循环和同步启动线程之间协调插件生命周期操作。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_plugins: set[str] = set()
        self._startup_active = False
        self._startup_token: PluginStartupLease | None = None

    @staticmethod
    def _normalize(plugin_id: str) -> str:
        return (plugin_id or "").strip().lower()

    def _try_acquire_plugin(
        self,
        plugin_id: str,
        startup_token: PluginStartupLease | None = None,
    ) -> bool:
        normalized_id = self._normalize(plugin_id)
        if not normalized_id:
            raise ValueError("插件ID不能为空")
        with self._condition:
            startup_token_matches = (
                startup_token is not None and startup_token is self._startup_token
            )
            # 启动期间只有当前 lease 的显式 token 可以取得逐插件资格。
            if (
                normalized_id in self._active_plugins
                or (self._startup_active and not startup_token_matches)
            ):
                return False
            self._active_plugins.add(normalized_id)
            return True

    def _release_plugin(self, plugin_id: str) -> None:
        normalized_id = self._normalize(plugin_id)
        with self._condition:
            self._active_plugins.discard(normalized_id)
            self._condition.notify_all()

    def _try_acquire_startup(self) -> PluginStartupLease | None:
        with self._condition:
            if self._startup_active or self._active_plugins:
                return None
            startup_token = PluginStartupLease()
            self._startup_active = True
            self._startup_token = startup_token
            return startup_token

    def _release_startup(self, startup_token: PluginStartupLease) -> None:
        with self._condition:
            # 延迟清理不得释放已经由新 owner 持有的启动 lease。
            if self._startup_token is not startup_token:
                return
            self._startup_token = None
            self._startup_active = False
            self._condition.notify_all()

    @asynccontextmanager
    async def hold(
        self,
        plugin_id: str,
        startup_token: PluginStartupLease | None = None,
    ) -> AsyncIterator[None]:
        """异步持有单个插件的生命周期资格，不在线程池中等待锁。"""
        while not self._try_acquire_plugin(plugin_id, startup_token):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._release_plugin(plugin_id)

    @asynccontextmanager
    async def hold_startup(self) -> AsyncIterator[PluginStartupLease]:
        """异步持有启动同步的全局资格，阻止安装请求穿过启动收口。"""
        startup_token: PluginStartupLease | None = None
        while startup_token is None:
            startup_token = self._try_acquire_startup()
            if startup_token is not None:
                break
            await asyncio.sleep(0.01)
        try:
            yield startup_token
        finally:
            self._release_startup(startup_token)


plugin_lifecycle = PluginLifecycleCoordinator()
