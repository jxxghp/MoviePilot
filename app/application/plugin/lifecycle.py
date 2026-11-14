"""
插件安装与启动同步之间共享的生命周期互斥。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.schemas.exception import PluginMutationRejectedError


class PluginStartupLease:
    """
    启动租约的不透明能力句柄，仅按对象身份由所属协调器认可。
    """

    __slots__ = ()


class PluginLifecycleCoordinator:
    """
    在事件循环和同步启动线程之间协调插件生命周期操作。
    """

    def __init__(self) -> None:
        """
        初始化逐插件资格、启动令牌与持有时刻。
        """
        self._condition = threading.Condition()
        self._active_plugins: set[str] = set()
        self._started_at: dict[str, float] = {}
        self._startup_active = False
        self._startup_started_at = 0.0
        self._startup_token: PluginStartupLease | None = None

    @staticmethod
    def _normalize(plugin_id: str) -> str:
        """
        按源码身份归一插件标识，避免大小写绕过互斥。
        """
        return (plugin_id or "").strip().lower()

    def _try_acquire_plugin(
        self,
        plugin_id: str,
        startup_token: PluginStartupLease | None = None,
    ) -> bool:
        """
        原子尝试逐插件资格，只有本次启动令牌可穿过启动屏障。
        """
        normalized_id = self._normalize(plugin_id)
        if not normalized_id:
            raise ValueError("插件ID不能为空")
        with self._condition:
            startup_token_matches = (
                startup_token is not None and startup_token is self._startup_token
            )
            # 启动期间只有当前租约的显式令牌可以取得逐插件资格。
            if (
                normalized_id in self._active_plugins
                or (self._startup_active and not startup_token_matches)
            ):
                return False
            self._active_plugins.add(normalized_id)
            self._started_at[normalized_id] = time.monotonic()
            return True

    def _release_plugin(self, plugin_id: str) -> None:
        """
        在实际执行结束后释放插件资格并移除耗时记录。
        """
        normalized_id = self._normalize(plugin_id)
        with self._condition:
            self._active_plugins.discard(normalized_id)
            self._started_at.pop(normalized_id, None)
            self._condition.notify_all()

    def _try_acquire_startup(self) -> PluginStartupLease | None:
        """
        仅在没有启动或逐插件持有者时签发启动租约。
        """
        with self._condition:
            if self._startup_active or self._active_plugins:
                return None
            startup_token = PluginStartupLease()
            self._startup_active = True
            self._startup_started_at = time.monotonic()
            self._startup_token = startup_token
            return startup_token

    def _release_startup(self, startup_token: PluginStartupLease) -> None:
        """
        仅由当前租约令牌释放启动屏障，避免旧清理误放行。
        """
        with self._condition:
            # 延迟清理不得释放已经由新持有者持有的启动租约。
            if self._startup_token is not startup_token:
                return
            self._startup_token = None
            self._startup_active = False
            self._condition.notify_all()

    def _busy_reason(self) -> str:
        """
        返回当前启动或逐插件资格的持有者及持续秒数。
        """
        with self._condition:
            owners = dict(self._started_at)
            if self._startup_active:
                owners["启动同步"] = self._startup_started_at
            now = time.monotonic()
            details = "、".join(f"{owner} 已执行 {now - started:.2f} 秒" for owner, started in owners.items())
            return f"插件生命周期准入正忙：{details}"

    @asynccontextmanager
    async def hold(
        self,
        plugin_id: str,
        startup_token: PluginStartupLease | None = None,
        *,
        timeout: float = 5.0,
    ) -> AsyncIterator[None]:
        """
        按预算异步等待单插件资格，超时不释放现有持有者。

        :param plugin_id: 需要取得执行资格的源码插件 ID
        :param startup_token: 当前启动租约令牌；外部请求无需传入
        :param timeout: 最长准入等待秒数，默认 5 秒
        :raises PluginMutationRejectedError: 超时仍未获准，操作尚未执行
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._try_acquire_plugin(plugin_id, startup_token):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginMutationRejectedError(f"操作插件 {plugin_id}", self._busy_reason())
            await asyncio.sleep(min(0.01, remaining))
            if time.monotonic() >= deadline:
                raise PluginMutationRejectedError(f"操作插件 {plugin_id}", self._busy_reason())
        try:
            yield
        finally:
            self._release_plugin(plugin_id)

    @asynccontextmanager
    async def hold_startup(self, *, timeout: float = 5.0) -> AsyncIterator[PluginStartupLease]:
        """
        有界取得启动同步资格，阻止安装请求穿过启动收口。

        :param timeout: 最长准入等待秒数，默认 5 秒
        :raises PluginMutationRejectedError: 预算耗尽时仍有真实持有者
        """
        deadline = time.monotonic() + max(0.0, timeout)
        startup_token: PluginStartupLease | None = None
        while startup_token is None:
            startup_token = self._try_acquire_startup()
            if startup_token is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginMutationRejectedError("启动同步插件", self._busy_reason())
            await asyncio.sleep(min(0.01, remaining))
            if time.monotonic() >= deadline:
                raise PluginMutationRejectedError("启动同步插件", self._busy_reason())
        try:
            yield startup_token
        finally:
            self._release_startup(startup_token)


plugin_lifecycle = PluginLifecycleCoordinator()
