"""插件可变事务停机准入的确定性测试。"""

import asyncio
import inspect
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.lifecycle.admission import (
    PluginMutationAdmission,
    PluginMutationRejectedError,
)
from app.runtime.extensions.lifecycle.system import reset_plugin_system
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import EventType


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器，并在用例结束后清除单例状态。"""
    key = (PluginManager, (), frozenset())
    Singleton._instances.pop(key, None)
    reset_plugin_system()
    manager = PluginManager()
    try:
        yield manager
    finally:
        Singleton._instances.pop(key, None)


def test_seal_rejects_new_root_but_allows_propagated_nested_lease() -> None:
    """封口拒绝无关调用，但已获准事务跨线程后的嵌套仍能完成。"""
    admission = PluginMutationAdmission()
    calls: list[str] = []

    def mutate(label: str) -> None:
        """在测试线程中尝试取得 lease 并记录副作用。"""
        with admission.hold(label):
            calls.append(label)

    with admission.hold("外层事务"):
        propagated = copy_context()
        assert admission.active_count == 1
        assert admission.seal() == 1
        with ThreadPoolExecutor(max_workers=2) as executor:
            executor.submit(propagated.run, mutate, "嵌套事务").result(timeout=1)
            rejected = executor.submit(mutate, "新事务")
            with pytest.raises(PluginMutationRejectedError):
                rejected.result(timeout=1)

        assert calls == ["嵌套事务"]
        assert admission.active_count == 1

    assert admission.active_count == 0
    assert admission.reopen() is True


@pytest.mark.asyncio
async def test_quiesce_timeout_retains_admitted_owner_and_nested_reload(
    plugin_manager: PluginManager,
    monkeypatch,
) -> None:
    """停机超时保留 active owner，且封口前获准的事务可完成内部重载。"""
    manager = plugin_manager
    entered = asyncio.Event()
    release = asyncio.Event()
    load = MagicMock(return_value={"DemoPlugin": PluginRuntimeStatus.ACTIVE})
    send_event = MagicMock()
    monkeypatch.setattr(manager, "_start_selective", load)
    monkeypatch.setattr(manager, "_stop_running_instances", MagicMock())
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.eventmanager.send_event",
        send_event,
    )
    monkeypatch.setattr(manager, "_quiesce_handlers_locked", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_finalize_locked", MagicMock(return_value=True))

    async def mutate() -> PluginRuntimeStatus:
        """持有外层 lease，等待封口后再调用嵌套 Manager 写入口。"""
        with manager.mutation("安装插件 DemoPlugin"):
            entered.set()
            await release.wait()
            return manager.reload_plugin("DemoPlugin")

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(
            "app.runtime.extensions.plugin_manager.ThreadHelper",
            lambda: SimpleNamespace(submit=executor.submit),
        )
        mutation_task = asyncio.create_task(mutate())
        await entered.wait()

        assert await manager.quiesce_plugins(timeout=0.01) is False
        owner = manager._plugin_quiesce_future
        assert owner is not None
        assert owner.done() is False
        assert manager._plugin_mutation_admission.active_count == 1
        assert manager.finalize_plugins() is False
        with pytest.raises(PluginMutationRejectedError):
            with manager.mutation("新的配置写入"):
                pass

        release.set()
        assert await mutation_task is PluginRuntimeStatus.ACTIVE
        assert await asyncio.wrap_future(owner) is True

    load.assert_called_once_with("DemoPlugin", None)
    send_event.assert_called_once_with(
        EventType.PluginReload,
        data={"plugin_id": "DemoPlugin"},
    )
    assert manager._plugin_mutation_admission.active_count == 0
    assert manager.finalize_plugins() is True


@pytest.mark.asyncio
async def test_quiesce_inside_mutation_fails_without_sealing(
    plugin_manager: PluginManager,
) -> None:
    """事务不能等待自身退出，快速失败时也不得误封口运行时。"""
    with plugin_manager.mutation("配置插件"):
        assert await plugin_manager.quiesce_plugins(timeout=0) is False
        assert plugin_manager._plugin_mutation_admission.accepting is True
        assert plugin_manager._plugin_runtime_closed is False


@pytest.mark.asyncio
async def test_sealed_manager_blocks_direct_persistent_mutations(
    plugin_manager: PluginManager,
    monkeypatch,
) -> None:
    """Manager 单项兼容入口在封口后返回既有失败形状且不触发底层副作用。"""
    storage = MagicMock()
    storage.async_write_config = AsyncMock()
    delete_data = MagicMock()
    sync_packages = MagicMock()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_storage",
        lambda: storage,
    )
    monkeypatch.setattr(plugin_manager, "_delete_plugin_data_locked", delete_data)
    monkeypatch.setattr(plugin_manager, "_sync_locked", sync_packages)
    plugin_manager._plugin_mutation_admission.seal()
    plugin_manager._plugin_runtime_closed = True

    assert plugin_manager.save_plugin_config("DemoPlugin", {}) is False
    assert await plugin_manager.async_save_plugin_config("DemoPlugin", {}) is False
    assert plugin_manager.delete_plugin_config("DemoPlugin") is False
    assert plugin_manager.delete_plugin_data("DemoPlugin") is False
    with pytest.raises(PluginMutationRejectedError):
        plugin_manager.sync()

    storage.write_config.assert_not_called()
    storage.async_write_config.assert_not_awaited()
    storage.delete_config.assert_not_called()
    delete_data.assert_not_called()
    sync_packages.assert_not_called()


def test_manager_exposes_no_virtual_plugin_instance_entries(
    plugin_manager: PluginManager,
) -> None:
    """插件分身入口不属于本宿主，管理器不得重新长出对应的可变事务。"""
    assert not hasattr(plugin_manager, "clone_plugin")
    assert not hasattr(plugin_manager, "_plugin_clone")
    # 实例删除只接受显式实例标识，没有按插件整体删除的单参 ABI
    assert "instance_id" in inspect.signature(
        plugin_manager.delete_plugin_instance
    ).parameters
