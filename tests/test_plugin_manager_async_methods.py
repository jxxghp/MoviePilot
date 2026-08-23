"""插件管理器异步方法的执行边界回归。"""

from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock

import pytest

from app.foundation.singleton import Singleton
from app.sdk.plugins import PluginManager


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.mark.asyncio
async def test_async_run_plugin_method_offloads_sync_plugin_method(
    plugin_manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步入口不得在事件循环内直接执行同步插件方法。"""
    calls: list[tuple[str, int]] = []

    def sync_method(value: int) -> int:
        calls.append(("sync", value))
        return value + 1

    plugin_manager.running_plugins["DemoPlugin"] = SimpleNamespace(
        sync_method=sync_method,
    )
    worker = AsyncMock(return_value=2)
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.run_in_threadpool_to_completion",
        worker,
    )

    assert await plugin_manager.async_run_plugin_method(
        "DemoPlugin", "sync_method", 1
    ) == 2
    worker.assert_awaited_once_with(sync_method, 1)
    assert calls == []


@pytest.mark.asyncio
async def test_async_run_plugin_method_keeps_async_plugin_method_on_loop(
    plugin_manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生协程插件方法继续直接等待，不额外占用同步 worker。"""
    async def async_method(value: int) -> int:
        return value + 1

    plugin_manager.running_plugins["DemoPlugin"] = SimpleNamespace(
        async_method=async_method,
    )
    worker = AsyncMock()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.run_in_threadpool_to_completion",
        worker,
    )

    assert await plugin_manager.async_run_plugin_method(
        "DemoPlugin", "async_method", 1
    ) == 2
    worker.assert_not_awaited()
