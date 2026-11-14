"""
慢插件钩子的准入拒绝、停机预算和真实资源所有权回归测试。
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.manager import PluginManager
from app.schemas.exception import PluginMutationRejectedError


@contextmanager
def _blocked_plugin(
    manager: Optional[PluginManager] = None, *, start: bool = True,
) -> Iterator[SimpleNamespace]:
    """
    在线程中阻塞真实 close 钩子，退出时始终放行并回收线程。
    """
    entered = threading.Event()
    release = threading.Event()
    database = MagicMock()
    calls = []

    class SlowPlugin:
        """
        以可控事件模拟未返回的第三方资源关闭钩子。
        """

        def close(self) -> None:
            """
            等待测试释放；兜底预算防止旧实现阻塞整个测试进程。
            """
            entered.set()
            calls.append("close")
            assert release.wait(timeout=3)

    instance = SlowPlugin()
    running = {"SlowPlugin": instance}
    lifecycle = PluginLifecycle(
        classes={"SlowPlugin": SlowPlugin}, running=running,
        load_plugins=lambda *_args: [], loadable_plugins=lambda: [],
        plugin_config=lambda _pid: {}, auth_checker=lambda _plugin: True,
        clear_modules=MagicMock(), clear_tools=MagicMock(),
        enable_events=MagicMock(), disable_events=MagicMock(),
        runtime_status_writer=MagicMock(), runtime_compatible=lambda _pid: True,
        database=lambda: database, log=MagicMock(), event_sender=MagicMock(),
    )
    if manager is not None:
        manager._plugin_lifecycle = lifecycle
        manager._running_plugins = running
    with ThreadPoolExecutor(max_workers=2) as executor:
        stop = manager.stop if manager is not None else lifecycle.stop
        future = executor.submit(stop, "SlowPlugin") if start else None
        try:
            if start:
                assert entered.wait(timeout=1)
            yield SimpleNamespace(
                lifecycle=lifecycle, executor=executor, future=future,
                running=running, instance=instance, database=database,
                entered=entered, release=release, calls=calls,
            )
        finally:
            release.set()
            if future is not None:
                future.result(timeout=1)


def _manager() -> PluginManager:
    """
    仅装配停机与准入所需字段，避免触碰单例和真实插件持久化服务。
    """
    manager = object.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_quiesce_future = None
    manager._plugin_service_quiesce_future = None
    manager._plugin_runtime_closed = False
    manager._plugin_mutation_admission = PluginMutationAdmission(
        activity=lambda: manager._plugin_lifecycle.activity(),
    )
    return manager


def test_slow_hook_rejects_other_lifecycle_without_waiting() -> None:
    """
    A 的钩子未退出时，B 必须明确拒绝执行且不能等到 A 释放。
    """
    with _blocked_plugin() as blocked:
        contender = blocked.executor.submit(blocked.lifecycle.start, "OtherPlugin")
        with pytest.raises(PluginMutationRejectedError, match="SlowPlugin.*close"):
            # 旧实现会在全局锁内等到此 Future 超时，因而使本断言失败。
            try:
                contender.result(timeout=0.3)
            except FutureTimeoutError:
                pytest.fail("慢钩子使其他插件的生命周期操作一直等待全局锁")
        assert blocked.running["SlowPlugin"] is blocked.instance
        blocked.database.release.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_budget_includes_contended_lifecycle_and_keeps_loop_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    停机预算包含准入等待，事件循环可继续调度且资源不能提前释放。
    """
    manager = _manager()
    with _blocked_plugin(manager) as blocked:
        monkeypatch.setattr(
            "app.runtime.extensions.plugin.manager.ThreadHelper",
            lambda: SimpleNamespace(submit=blocked.executor.submit),
        )

        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_later(0.01, heartbeat.set)
        started = time.monotonic()
        result = await manager.quiesce_plugins(timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert result is False
        assert heartbeat.is_set()
        assert blocked.running["SlowPlugin"] is blocked.instance
        assert manager.finalize_plugins() is False
        blocked.database.release.assert_not_called()
        assert blocked.future.done() is False


def test_busy_mutation_rejects_before_side_effects_and_preserves_owner() -> None:
    """
    根事务在写入前快速拒绝，副作用、owner 和数据库均保持真实状态。
    """
    manager = _manager()
    changed = []
    with _blocked_plugin(manager) as blocked:
        with pytest.raises(PluginMutationRejectedError, match="SlowPlugin.*close"):
            with manager.mutation("保存 OtherPlugin 配置"):
                changed.append(True)
        assert changed == []
        assert manager._plugin_mutation_admission.active_count == 1
        assert manager._plugin_mutation_admission.wait_until_idle(timeout=0.01) is False
        activity = manager.get_plugin_lifecycle_activity()
        assert activity.plugin_id == "SlowPlugin"
        assert activity.hook == "close"
        assert activity.elapsed_seconds >= activity.hook_elapsed_seconds > 0
        assert blocked.future.done() is False
    assert manager._plugin_mutation_admission.wait_until_idle(timeout=0) is True
    assert manager.get_plugin_lifecycle_activity() is None
    with manager.mutation("重试保存 OtherPlugin 配置"):
        changed.append(True)
    assert changed == [True]


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_real_hook_owner_and_reuses_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    调用方取消或再次超时都不能重复执行钩子、释放实例或销毁数据库。
    """
    manager = _manager()
    with _blocked_plugin(manager, start=False) as blocked:
        monkeypatch.setattr(
            "app.runtime.extensions.plugin.manager.ThreadHelper",
            lambda: SimpleNamespace(submit=blocked.executor.submit),
        )
        assert await manager.quiesce_plugins(timeout=1) is True
        task = asyncio.create_task(manager.quiesce_plugin_services(timeout=1))
        try:
            assert await asyncio.to_thread(blocked.entered.wait, 1)
            owner = manager._plugin_service_quiesce_future
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert owner is not None and not owner.done()
            assert await manager.quiesce_plugin_services(timeout=0.01) is False
            assert manager._plugin_service_quiesce_future is owner
            assert blocked.calls == ["close"]
            assert manager.finalize_plugins() is False
            assert manager.reopen_plugins() is False
            assert blocked.running["SlowPlugin"] is blocked.instance
            blocked.database.release.assert_not_called()
            activity = manager.get_plugin_lifecycle_activity()
            assert activity.operation == "quiesce_services"
            assert activity.plugin_id == "SlowPlugin"
            assert activity.hook == "close"
            blocked.release.set()
            assert await asyncio.wrap_future(owner) is True
            assert manager.finalize_plugins() is True
            assert blocked.running == {}
            blocked.database.release.assert_called_once_with("SlowPlugin")
        finally:
            blocked.release.set()
            if not task.done():
                await task


@pytest.mark.asyncio
async def test_runtime_summary_exposes_real_hook_and_does_not_report_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    摘要在后台收敛标记为空闲时，仍能识别实际执行中的插件钩子。
    """
    from app.api.endpoints import plugin as endpoint
    from app.schemas.plugin import PluginRuntimeStatus

    with _blocked_plugin() as blocked:
        manager = SimpleNamespace(
            get_plugin_runtime_statuses=lambda: {"SlowPlugin": PluginRuntimeStatus.READY},
            get_plugin_restart_requirements=lambda: {},
            get_plugin_gil_fallbacks=lambda: [],
            get_plugin_runtime_generation=lambda: 3,
            is_plugin_settling=lambda: False,
            get_plugin_lifecycle_activity=blocked.lifecycle.activity,
        )
        monkeypatch.setattr(endpoint, "get_plugin_manager", lambda: manager)
        monkeypatch.setattr(
            endpoint, "get_configured_system_config",
            lambda: SimpleNamespace(get=lambda _key: ["SlowPlugin"]),
        )
        summary = await endpoint.runtime_status(None)
        assert summary.ready is False
        assert summary.pending_count == 1
        assert summary.active_operation.plugin_id == "SlowPlugin"
        assert summary.active_operation.hook == "close"
        assert summary.model_dump()["active_operation"]["elapsed_seconds"] >= 0
    summary = await endpoint.runtime_status(None)
    assert summary.ready is True
    assert summary.active_operation is None
