import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.config import global_vars
from app.runtime.extensions.contract.dependency import (
    PluginDependencyClassification,
    PluginDependencyInstallResult,
)
from app.runtime.extensions.lifecycle.admission import PluginMutationAdmission
from app.runtime.extensions.lifecycle.system import reset_plugin_system
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus
from app.startup import plugins_initializer

# 插件管理器启动监控线程的私有入口，用例需要在不真正起线程的前提下观察调用
_START_MONITOR = "_PluginManager__start_monitor"


def _reset_plugin_manager() -> None:
    """清除插件管理器单例，保证构造时序测试彼此隔离。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _patch_settings(monkeypatch, *, dev: bool, auto_reload: bool) -> None:
    """把插件管理器读取的运行配置替换为用例可控的最小集合。"""
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=dev,
            PLUGIN_AUTO_RELOAD=auto_reload,
            ROOT_PATH=MagicMock(),
        ),
    )


@pytest.mark.parametrize(
    ("dev", "auto_reload"),
    ((True, False), (False, True)),
)
def test_plugin_manager_constructor_does_not_start_monitor_before_runtime(
    monkeypatch,
    dev: bool,
    auto_reload: bool,
) -> None:
    """组合根尚未装配时，构造插件管理器不得提前启动开发监控线程。"""
    _reset_plugin_manager()
    reset_plugin_system()
    start = MagicMock()
    _patch_settings(monkeypatch, dev=dev, auto_reload=auto_reload)
    monkeypatch.setattr(PluginManager, _START_MONITOR, start)

    PluginManager()

    start.assert_not_called()
    _reset_plugin_manager()


def test_init_plugins_starts_monitor_after_runtime_and_routes(monkeypatch) -> None:
    """启动阶段只加载依赖已就绪的插件，再开放路由和文件监控。"""
    order: list[str] = []
    manager = MagicMock()
    manager.plugins = {}
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=("DependencyPending",),
        missing_source=("SourcePending",),
    )
    manager.reopen_plugins.side_effect = lambda: order.append("reopen") or True
    manager.start.side_effect = lambda plugin_id: order.append(f"plugin:{plugin_id}")
    manager.start_monitor.side_effect = lambda **_kwargs: order.append("monitor")
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_services",
        lambda: order.append("services"),
    )
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(
        plugins_initializer,
        "register_plugin_api",
        lambda: order.append("routes"),
    )

    plugins_initializer.init_plugins()

    assert order == [
        "services",
        "reopen",
        "plugin:ReadyPlugin",
        "routes",
        "monitor",
    ]
    manager.reopen_plugins.assert_called_once_with()
    manager.set_plugin_settling.assert_called_once_with(True)
    manager.start_monitor.assert_called_once_with(reopen=True)


def test_plugin_manager_projects_dependency_classification_to_runtime_status() -> None:
    """真实管理器按分类字段写入三类启动状态，避免测试替身掩盖字段漂移。"""
    _reset_plugin_manager()
    manager = PluginManager()

    manager.apply_plugin_dependency_classification(
        PluginDependencyClassification(
            ready=("ReadyPlugin",),
            missing_dependencies=("DependencyPending",),
            missing_source=("SourcePending",),
        )
    )

    assert manager.get_plugin_runtime_statuses() == {
        "ReadyPlugin": PluginRuntimeStatus.READY,
        "DependencyPending": PluginRuntimeStatus.DEPENDENCY_PENDING,
        "SourcePending": PluginRuntimeStatus.SOURCE_MISSING,
    }
    _reset_plugin_manager()


def test_plugin_manager_keeps_active_plugin_status_on_reclassification() -> None:
    """已激活插件重新分类时保持 active，不被打回 ready。"""
    _reset_plugin_manager()
    manager = PluginManager()
    manager._plugin_registry.running["ActivePlugin"] = object()
    manager._plugin_registry.set_runtime_status(
        "ActivePlugin",
        PluginRuntimeStatus.ACTIVE,
    )

    manager.apply_plugin_dependency_classification(
        PluginDependencyClassification(
            ready=("ActivePlugin",),
            missing_dependencies=(),
            missing_source=(),
        )
    )

    assert manager.get_plugin_runtime_statuses()["ActivePlugin"] is (
        PluginRuntimeStatus.ACTIVE
    )
    _reset_plugin_manager()


def test_plugin_manager_promotes_running_dependency_after_recovery() -> None:
    """依赖恢复后，运行中的插件状态必须允许后台流程触发重载。"""
    _reset_plugin_manager()
    manager = PluginManager()
    manager._plugin_registry.running["DependencyRecovered"] = object()
    manager._plugin_registry.set_runtime_status(
        "DependencyRecovered",
        PluginRuntimeStatus.DEPENDENCY_PENDING,
    )

    manager.apply_plugin_dependency_classification(
        PluginDependencyClassification(
            ready=("DependencyRecovered",),
            missing_dependencies=(),
            missing_source=(),
        )
    )

    assert manager.get_plugin_runtime_statuses()["DependencyRecovered"] is (
        PluginRuntimeStatus.READY
    )
    _reset_plugin_manager()


def _patch_sync_plugins(
    monkeypatch,
    manager: MagicMock,
    running_ids: list[str] | None = None,
) -> MagicMock:
    """隔离后台执行器并返回动态路由注册替身。"""
    async def execute(_loop, task_func, _task_name):
        return task_func()

    running = list(running_ids or [])
    register = MagicMock()
    manager.plugins = {}
    manager.get_running_plugin_ids.side_effect = lambda: list(running)
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )
    monkeypatch.setattr(plugins_initializer, "configure_plugin_services", lambda: None)
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugins_initializer, "execute_task", execute)
    monkeypatch.setattr(plugins_initializer, "register_plugin_api", register)
    dependency_result = (
        manager.async_install_plugin_missing_dependencies_with_status.return_value
    )
    manager.async_install_plugin_missing_dependencies_with_status = AsyncMock(
        return_value=dependency_result,
    )
    manager.get_plugin_runtime_statuses.return_value = {}
    manager.start.side_effect = running.append
    return register


@pytest.mark.asyncio
async def test_sync_plugins_rejects_before_configuring_mutable_services(
    monkeypatch,
) -> None:
    """启动后同步在 admission 封口后不重装配服务、不写包或运行态。"""
    admission = PluginMutationAdmission()
    admission.seal()
    manager = MagicMock()
    manager.mutation.side_effect = admission.hold
    configure = MagicMock()
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugins_initializer, "configure_plugin_services", configure)

    assert await plugins_initializer.sync_plugins() is False

    configure.assert_not_called()
    manager.set_plugin_settling.assert_not_called()
    manager.sync.assert_not_called()


@pytest.mark.asyncio
async def test_sync_plugins_activates_ready_plugins_when_dependencies_fail(
    monkeypatch,
) -> None:
    """依赖恢复失败时仍激活无关的已就绪插件。"""
    manager = MagicMock()
    manager.sync.return_value = ["demo"]
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=False)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=("DependencyPending",),
        missing_source=(),
    )
    register = _patch_sync_plugins(monkeypatch, manager)

    assert await plugins_initializer.sync_plugins() is True

    manager.start.assert_called_once_with("ReadyPlugin")
    manager.reload_plugin.assert_not_called()
    register.assert_called_once_with("ReadyPlugin")


@pytest.mark.asyncio
async def test_sync_plugins_loads_only_plugins_that_become_ready(
    monkeypatch,
) -> None:
    """后台依赖恢复后只启动尚未运行且当前已就绪的插件。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin", "DependencyRecovered"),
        missing_dependencies=(),
        missing_source=("SourcePending",),
    )
    register = _patch_sync_plugins(monkeypatch, manager, ["ReadyPlugin"])

    assert await plugins_initializer.sync_plugins() is True

    manager.start.assert_called_once_with("DependencyRecovered")
    manager.reload_plugin.assert_not_called()
    register.assert_called_once_with("DependencyRecovered")


@pytest.mark.asyncio
async def test_sync_plugins_reloads_only_updated_running_plugins(monkeypatch) -> None:
    """源码同步只重载对应运行实例，不重启其他插件。"""
    manager = MagicMock()
    manager.sync.return_value = ["UpdatedPlugin"]
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("StablePlugin", "UpdatedPlugin"),
        missing_dependencies=(),
        missing_source=(),
    )
    register = _patch_sync_plugins(
        monkeypatch, manager, ["StablePlugin", "UpdatedPlugin"]
    )

    assert await plugins_initializer.sync_plugins() is True

    manager.reload_plugin.assert_called_once_with("UpdatedPlugin")
    manager.start.assert_not_called()
    register.assert_called_once_with("UpdatedPlugin")


@pytest.mark.asyncio
async def test_sync_plugins_reloads_running_plugin_after_dependency_recovery(
    monkeypatch,
) -> None:
    """依赖恢复后，已运行的旧实例必须切换到新源码。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("DependencyRecovered",),
        missing_dependencies=(),
        missing_source=(),
    )
    register = _patch_sync_plugins(monkeypatch, manager, ["DependencyRecovered"])
    manager.get_plugin_runtime_statuses.return_value = {
        "DependencyRecovered": PluginRuntimeStatus.DEPENDENCY_PENDING,
    }

    assert await plugins_initializer.sync_plugins() is True

    manager.reload_plugin.assert_called_once_with("DependencyRecovered")
    manager.start.assert_not_called()
    register.assert_called_once_with("DependencyRecovered")


@pytest.mark.asyncio
async def test_sync_plugins_keeps_runtime_when_nothing_changed(monkeypatch) -> None:
    """源码和依赖均无变化时保留首次初始化结果。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=(),
        missing_source=(),
    )
    register = _patch_sync_plugins(monkeypatch, manager, ["ReadyPlugin"])

    assert await plugins_initializer.sync_plugins() is False

    manager.start.assert_not_called()
    manager.reload_plugin.assert_not_called()
    register.assert_not_called()


@pytest.mark.asyncio
async def test_sync_plugins_keeps_event_loop_responsive_during_activation(
    monkeypatch,
) -> None:
    """插件初始化运行在线程池时，Web 事件循环仍可继续调度。"""
    manager = MagicMock()
    manager.plugins = {}
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("SlowPlugin",),
        missing_dependencies=(),
        missing_source=(),
    )
    manager.get_running_plugin_ids.return_value = []
    manager.get_plugin_runtime_statuses.return_value = {}
    activation_started = threading.Event()

    def slow_start(_plugin_id: str) -> None:
        """占用线程池工作线程，模拟一次耗时的插件导入。"""
        activation_started.set()
        time.sleep(0.1)

    manager.start.side_effect = slow_start
    manager.async_install_plugin_missing_dependencies_with_status = AsyncMock(
        return_value=PluginDependencyInstallResult(missing=[], success=True),
    )
    monkeypatch.setattr(plugins_initializer, "configure_plugin_services", lambda: None)
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugins_initializer, "register_plugin_api", MagicMock())
    monkeypatch.setattr(
        plugins_initializer.global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )

    sync_task = asyncio.create_task(plugins_initializer.sync_plugins())
    assert await asyncio.to_thread(activation_started.wait, 1)
    assert sync_task.done() is False
    assert await sync_task is True


@pytest.mark.parametrize(
    ("dev", "auto_reload", "expected_calls"),
    ((True, False, 1), (False, True, 1), (False, False, 0)),
)
def test_start_monitor_respects_runtime_configuration(
    monkeypatch,
    dev: bool,
    auto_reload: bool,
    expected_calls: int,
) -> None:
    """首次启动只在开发模式或插件自动重载启用时创建监控线程。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=dev, auto_reload=auto_reload)
    manager = PluginManager()
    start = MagicMock()
    monkeypatch.setattr(manager, _START_MONITOR, start)

    manager.start_monitor()

    assert start.call_count == expected_calls
    _reset_plugin_manager()


def test_plugin_monitor_waits_until_dependency_settlement(monkeypatch) -> None:
    """后台依赖收敛期间不启动文件监控，避免源码写入触发重复重载。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=True, auto_reload=False)
    manager = PluginManager()
    start = MagicMock()
    stop = MagicMock()
    monkeypatch.setattr(manager, _START_MONITOR, start)
    monkeypatch.setattr(manager, "stop_monitor", stop)

    manager.set_plugin_settling(True)
    manager.start_monitor()
    manager.reload_monitor()

    start.assert_not_called()
    stop.assert_called_once_with()

    manager.set_plugin_settling(False)
    manager.start_monitor()

    start.assert_called_once_with()
    _reset_plugin_manager()


def test_monitor_stop_keeps_thread_owned_until_it_really_exits(monkeypatch) -> None:
    """停止超时后保留活线程引用，使后续停机调用仍能继续等待。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=True, auto_reload=False)
    manager = PluginManager()
    started = threading.Event()
    release = threading.Event()

    def runner() -> None:
        """模拟暂时无法响应停止事件的文件监控循环。"""
        started.set()
        release.wait()

    monkeypatch.setattr(manager, "_run_file_watcher", runner)
    manager.start_monitor()
    assert started.wait(1)
    owned_thread = manager._monitor_thread

    try:
        assert manager.stop_monitor(timeout=0.01) is False
        assert manager._monitor_thread is owned_thread
        assert owned_thread is not None and owned_thread.is_alive()
    finally:
        release.set()
        assert manager.stop_monitor(timeout=1) is True

    assert manager._monitor_thread is None
    assert manager.stop_monitor(timeout=0) is True
    _reset_plugin_manager()


def test_monitor_stop_counts_lifecycle_lock_wait_in_timeout(monkeypatch) -> None:
    """并发重建占用生命周期锁时，停止调用不得突破自身预算。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=False, auto_reload=False)
    manager = PluginManager()
    lock_acquired = threading.Event()
    release = threading.Event()

    def hold_lifecycle_lock() -> None:
        """模拟配置重载在停止请求到达时仍持有生命周期锁。"""
        with manager._monitor_lifecycle_lock:
            lock_acquired.set()
            release.wait()

    holder = threading.Thread(target=hold_lifecycle_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(1)
    started_at = time.monotonic()

    try:
        assert manager.stop_monitor(timeout=0.01) is False
        assert time.monotonic() - started_at < 0.2
    finally:
        release.set()
        holder.join(timeout=1)

    assert holder.is_alive() is False
    assert manager.stop_monitor(timeout=0) is True
    _reset_plugin_manager()


def test_shutdown_seal_blocks_reload_between_stop_and_start(monkeypatch) -> None:
    """停机封口落在热重载停启间隙时，旧重载不得再创建监控线程。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=True, auto_reload=False)
    manager = PluginManager()
    reload_stopped = threading.Event()
    resume_reload = threading.Event()
    runner_started = threading.Event()
    monkeypatch.setattr(manager, "_run_file_watcher", runner_started.set)
    original_stop = manager.stop_monitor

    def pause_after_stop(timeout: float = 5.0) -> bool:
        """把配置热重载稳定暂停在旧线程已停、新线程未启的窗口。"""
        stopped = original_stop(timeout=timeout)
        reload_stopped.set()
        resume_reload.wait()
        return stopped

    monkeypatch.setattr(manager, "stop_monitor", pause_after_stop)
    reload_thread = threading.Thread(target=manager.reload_monitor, daemon=True)
    reload_thread.start()
    assert reload_stopped.wait(1)

    try:
        assert manager.close_monitor(timeout=1) is True
        resume_reload.set()
        reload_thread.join(timeout=1)
        assert reload_thread.is_alive() is False
        assert runner_started.is_set() is False
        assert manager._monitor_thread is None
    finally:
        resume_reload.set()
        reload_thread.join(timeout=1)

    assert manager._reopen_monitor() is True
    manager.start_monitor()
    assert runner_started.wait(1)
    assert manager.close_monitor(timeout=1) is True
    _reset_plugin_manager()


def test_plugin_manager_stop_monitor_returns_thread_join_result(monkeypatch) -> None:
    """管理器停止入口透传线程收口结果和调用预算，且只有 close 才封口。"""
    _reset_plugin_manager()
    reset_plugin_system()
    manager = PluginManager()
    stop = MagicMock(return_value=False)
    monkeypatch.setattr(manager, "_stop_monitor_with_budget", stop)

    try:
        assert manager.stop_monitor(timeout=0.25) is False
        stop.assert_called_once_with(timeout=0.25, close=False)

        stop.reset_mock()
        assert manager.close_monitor(timeout=0.25) is False
        stop.assert_called_once_with(timeout=0.25, close=True)
    finally:
        _reset_plugin_manager()


def test_plugin_manager_start_monitor_can_reopen_new_lifespan(monkeypatch) -> None:
    """新应用生命周期可显式解除封口，再按运行配置启动监控。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=True, auto_reload=False)
    manager = PluginManager()
    reopen = MagicMock(return_value=True)
    start = MagicMock()
    monkeypatch.setattr(manager, "_reopen_monitor", reopen)
    monkeypatch.setattr(manager, _START_MONITOR, start)

    try:
        manager.start_monitor(reopen=True)
        reopen.assert_called_once_with()
        start.assert_called_once_with()
    finally:
        _reset_plugin_manager()


def test_plugin_manager_start_monitor_skips_start_when_reopen_refused(
    monkeypatch,
) -> None:
    """旧生命周期尚未真正收口时，显式重开失败必须放弃本次启动。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=True, auto_reload=False)
    manager = PluginManager()
    reopen = MagicMock(return_value=False)
    start = MagicMock()
    monkeypatch.setattr(manager, "_reopen_monitor", reopen)
    monkeypatch.setattr(manager, _START_MONITOR, start)

    try:
        manager.start_monitor(reopen=True)
        reopen.assert_called_once_with()
        start.assert_not_called()
    finally:
        _reset_plugin_manager()


def test_stop_plugin_monitor_does_not_materialize_manager() -> None:
    """插件运行时尚未创建时，独立停机入口直接视为已完成。"""
    _reset_plugin_manager()

    assert plugins_initializer.stop_plugin_monitor(timeout=0) is True
    assert PluginManager.get_existing_instance() is None


def test_stop_plugin_monitor_returns_existing_manager_result(monkeypatch) -> None:
    """启动层入口只操作既有管理器，并透传超时失败。"""
    manager = MagicMock()
    manager.close_monitor.return_value = False
    manager_type = SimpleNamespace(get_existing_instance=lambda: manager)
    monkeypatch.setattr(plugins_initializer, "PluginManager", manager_type)

    assert plugins_initializer.stop_plugin_monitor(timeout=0.25) is False
    manager.close_monitor.assert_called_once_with(timeout=0.25)


@pytest.mark.asyncio
async def test_two_phase_plugin_shutdown_does_not_materialize_manager() -> None:
    """两阶段入口在插件管理器尚未创建时都直接视为已收敛。"""
    _reset_plugin_manager()

    assert await plugins_initializer.quiesce_plugins(timeout=0) is True
    assert plugins_initializer.finalize_plugins() is True
    assert PluginManager.get_existing_instance() is None


@pytest.mark.asyncio
async def test_quiesce_timeout_retains_future_owner_until_worker_finishes(
    monkeypatch,
) -> None:
    """同步插件 hook 超时后必须保留 Future，且未结束前拒绝卸载实例。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=False,
            PLUGIN_AUTO_RELOAD=False,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()
    started = threading.Event()
    release = threading.Event()

    def blocking_quiesce() -> bool:
        """模拟无法由 asyncio 取消的同步旧插件 hook。"""
        started.set()
        release.wait(timeout=2)
        return True

    finalize = MagicMock(return_value=True)
    monkeypatch.setattr(manager, "_quiesce_handlers_locked", blocking_quiesce)
    monkeypatch.setattr(manager, "_finalize_locked", finalize)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            thread_helper = SimpleNamespace(submit=executor.submit)
            monkeypatch.setattr(
                "app.runtime.extensions.plugin_manager.ThreadHelper",
                lambda: thread_helper,
            )

            assert await manager.quiesce_plugins(timeout=0.01) is False
            assert started.is_set()
            owner = manager._plugin_quiesce_future
            assert owner is not None
            assert owner.done() is False
            assert manager.finalize_plugins() is False
            finalize.assert_not_called()

            release.set()
            assert await asyncio.wrap_future(owner) is True
            assert manager.finalize_plugins() is True
            finalize.assert_called_once_with()
    finally:
        release.set()
        _reset_plugin_manager()


@pytest.mark.asyncio
async def test_quiesce_seals_runtime_until_new_lifespan_reopens(monkeypatch) -> None:
    """屏障前封口后 start/reload/config 不能重开 producer，新 lifespan 可显式恢复。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEBUG=False,
            DEV=False,
            PLUGIN_AUTO_RELOAD=False,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()
    load = MagicMock(return_value={"DemoPlugin": PluginRuntimeStatus.ACTIVE})
    unload = MagicMock(return_value=None)
    resolve_instance = MagicMock(return_value=None)
    remove = MagicMock()
    set_runtime_status = MagicMock()
    classify = MagicMock()
    monkeypatch.setattr(manager, "_quiesce_handlers_locked", lambda: True)
    monkeypatch.setattr(manager, "_start_selective", load)
    monkeypatch.setattr(manager, "_stop_running_instances", unload)
    monkeypatch.setattr(manager, "_resolve_instance_key", resolve_instance)
    monkeypatch.setattr(manager._plugin_registry, "remove", remove)
    monkeypatch.setattr(manager._plugin_registry, "set_runtime_status", set_runtime_status)
    monkeypatch.setattr(manager, "classify_plugins", classify)

    class DemoPlugin:
        """代表 quiesce 后仍由严格生命周期持有的运行实例。"""

    plugin_instance = DemoPlugin()
    manager._plugins["DemoPlugin"] = DemoPlugin
    manager._running_plugins["DemoPlugin"] = plugin_instance

    try:
        assert await manager.quiesce_plugins(timeout=1) is True

        assert manager.start("DemoPlugin") == {
            "DemoPlugin": PluginRuntimeStatus.LOAD_FAILED
        }
        assert manager.stop("DemoPlugin") is None
        assert manager.remove_plugin("DemoPlugin") is None
        assert (
            manager.reload_plugin("DemoPlugin")
            is PluginRuntimeStatus.LOAD_FAILED
        )
        manager.init_plugin("DemoPlugin", {})
        manager.init_config()
        load.assert_not_called()
        unload.assert_not_called()
        resolve_instance.assert_not_called()
        remove.assert_not_called()
        set_runtime_status.assert_not_called()
        classify.assert_not_called()
        assert manager._plugins["DemoPlugin"] is DemoPlugin
        assert manager._running_plugins["DemoPlugin"] is plugin_instance

        assert manager.reopen_plugins() is False
        manager._plugins.clear()
        manager._running_plugins.clear()
        assert manager.reopen_plugins() is True
        assert manager.start("DemoPlugin") == {
            "DemoPlugin": PluginRuntimeStatus.ACTIVE
        }
        assert (
            manager.reload_plugin("DemoPlugin")
            is PluginRuntimeStatus.ACTIVE
        )
    finally:
        _reset_plugin_manager()


def test_plugin_monitor_skips_installing_plugin_until_package_write_finishes(
    monkeypatch,
    tmp_path,
) -> None:
    """安装替换目录期间，文件事件不得抢先导入未完成的插件包。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=False, auto_reload=False)
    manager = PluginManager()
    reload_plugin = MagicMock()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_system",
        lambda: SimpleNamespace(dependency_manifest_status=lambda _path: None),
    )
    monkeypatch.setattr(manager, "_get_federated_plugin_change", lambda _path: None)
    monkeypatch.setattr(
        manager, "_get_plugin_target_from_path", lambda _path: ("DemoPlugin", None)
    )
    monkeypatch.setattr(manager, "reload_plugin", reload_plugin)

    with manager.suppress_plugin_monitor("DemoPlugin"):
        manager._process_watch_changes(
            {("modified", str(tmp_path / "demo" / "plugin.py"))}
        )

    reload_plugin.assert_not_called()

    manager._process_watch_changes({("modified", str(tmp_path / "demo" / "plugin.py"))})

    reload_plugin.assert_called_once_with("DemoPlugin")
    _reset_plugin_manager()


def test_plugin_monitor_suppression_is_reference_counted(monkeypatch) -> None:
    """同一插件的重叠写入必须等最后一个事务退出后才解除监控抑制。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=False, auto_reload=False)
    manager = PluginManager()

    with manager.suppress_plugin_monitor("DemoPlugin"):
        assert manager.is_plugin_monitor_suppressed("demoplugin") is True
        with manager.suppress_plugin_monitor("demoplugin"):
            assert manager.is_plugin_monitor_suppressed("DemoPlugin") is True
        assert manager.is_plugin_monitor_suppressed("DemoPlugin") is True

    assert manager.is_plugin_monitor_suppressed("DemoPlugin") is False
    _reset_plugin_manager()


def test_config_change_reloads_monitor(monkeypatch) -> None:
    """配置热更新继续使用重建语义，不复用首次启动入口。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=False, auto_reload=False)
    manager = PluginManager()
    reload_monitor = MagicMock()
    manager.reload_monitor = reload_monitor

    manager.on_config_changed()

    reload_monitor.assert_called_once_with()
    _reset_plugin_manager()


def test_stop_plugins_stops_monitor_before_plugin_runtime(monkeypatch) -> None:
    """关闭时先隔离文件变化，再停止插件实例。"""
    order: list[str] = []
    manager = MagicMock()
    manager.stop_monitor.side_effect = lambda: order.append("monitor") or True
    manager.stop.side_effect = lambda: order.append("plugins")
    manager_type = SimpleNamespace(get_existing_instance=lambda: manager)
    monkeypatch.setattr(plugins_initializer, "PluginManager", manager_type)

    assert plugins_initializer.stop_plugins() is True

    assert order == ["monitor", "plugins"]


def test_stop_plugins_still_stops_runtime_when_monitor_stop_fails(monkeypatch) -> None:
    """监控线程停止异常不得阻止插件实例释放资源。"""
    manager = MagicMock()
    manager.stop_monitor.side_effect = RuntimeError("monitor stop failed")
    manager_type = SimpleNamespace(get_existing_instance=lambda: manager)
    monkeypatch.setattr(plugins_initializer, "PluginManager", manager_type)

    assert plugins_initializer.stop_plugins() is False

    manager.stop.assert_called_once_with()


def test_stop_plugins_does_not_materialize_manager() -> None:
    """插件初始化尚未创建管理器时，失败清理不得反向构造运行时。"""
    _reset_plugin_manager()

    assert plugins_initializer.stop_plugins() is True

    assert PluginManager.get_existing_instance() is None


def test_stop_plugins_remains_idempotent(monkeypatch) -> None:
    """兼容停机入口可重复调用，并保持每次先停监控再停插件。"""
    order: list[str] = []
    manager = MagicMock()
    manager.stop_monitor.side_effect = lambda: order.append("monitor") or True
    manager.stop.side_effect = lambda: order.append("plugins")
    manager_type = SimpleNamespace(get_existing_instance=lambda: manager)
    monkeypatch.setattr(plugins_initializer, "PluginManager", manager_type)

    assert plugins_initializer.stop_plugins() is True
    assert plugins_initializer.stop_plugins() is True

    assert order == ["monitor", "plugins", "monitor", "plugins"]


def test_plugin_manager_legacy_stop_preserves_none_return() -> None:
    """公共 PluginManager.stop 委托单阶段停机后保持历史 None 返回 ABI。"""
    manager = object.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_runtime_closed = False
    manager._plugin_quiesce_future = None
    manager._plugin_mutation_admission = PluginMutationAdmission()
    manager._stop_running_instances = MagicMock(return_value=True)

    assert PluginManager.stop(manager, "DemoPlugin") is None

    manager._stop_running_instances.assert_called_once_with(
        "DemoPlugin",
        None,
        skip_service_hooks=False,
    )


def test_config_reload_continues_after_legacy_stop() -> None:
    """配置热重载保持旧行为，stop 的 None 返回不得阻止重新分类和启动。"""
    manager = object.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_runtime_closed = False
    manager._plugin_mutation_admission = PluginMutationAdmission()
    manager.stop = MagicMock(return_value=None)
    manager.classify_plugins = MagicMock(
        return_value=PluginDependencyClassification(
            ready=("ReadyPlugin",),
            missing_dependencies=(),
            missing_source=(),
        )
    )
    manager.apply_plugin_dependency_classification = MagicMock()
    manager.start = MagicMock()

    PluginManager.init_config(manager)

    manager.stop.assert_called_once_with()
    manager.classify_plugins.assert_called_once_with()
    manager.apply_plugin_dependency_classification.assert_called_once_with(
        manager.classify_plugins.return_value
    )
    manager.start.assert_called_once_with("ReadyPlugin")


def test_remove_plugin_clears_registry_after_legacy_stop(monkeypatch) -> None:
    """卸载路径保持忽略 stop 的 None 返回值并继续清理注册表。"""
    _reset_plugin_manager()
    reset_plugin_system()
    _patch_settings(monkeypatch, dev=False, auto_reload=False)
    manager = PluginManager()
    remove = MagicMock()
    monkeypatch.setattr(manager._plugin_registry, "remove", remove)

    try:
        assert manager.stop("DemoPlugin") is None
        assert manager.remove_plugin("DemoPlugin") is None

        remove.assert_called_with("DemoPlugin")
    finally:
        _reset_plugin_manager()
