"""插件可变事务停机准入的确定性测试。"""

import asyncio
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin.admission import (
    PluginMutationAdmission,
    PluginMutationRejectedError,
)
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.system import reset_plugin_system
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import EventType


class _GatedCondition:
    """让指定测试线程在取得真实 Condition 前停住。"""

    def __init__(self, blocked_thread_prefix: str) -> None:
        """初始化内部 Condition 和可控的竞态闸门。"""
        self._condition = threading.Condition()
        self._blocked_thread_prefix = blocked_thread_prefix
        self._blocked = False
        self.attempted = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        """在目标线程首次进入时等待测试放行。"""
        if (
            threading.current_thread().name.startswith(self._blocked_thread_prefix)
            and not self._blocked
        ):
            self._blocked = True
            self.attempted.set()
            if not self.release.wait(timeout=1):
                raise TimeoutError("测试未及时放行 Condition")
        return self._condition.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        """把上下文退出委托给内部 Condition。"""
        return self._condition.__exit__(exc_type, exc_value, traceback)

    def wait(self) -> bool:
        """等待 admission 活动计数变化。"""
        return self._condition.wait()

    def notify_all(self) -> None:
        """唤醒全部等待 admission 空闲的线程。"""
        self._condition.notify_all()


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


def test_stale_propagated_context_is_rechecked_atomically_after_seal() -> None:
    """外层退出后才取得 Condition 的复制上下文不得冒充嵌套事务。"""
    admission = PluginMutationAdmission()
    calls: list[str] = []
    outer = admission.hold("外层事务")
    outer.__enter__()
    propagated = copy_context()
    condition = _GatedCondition("stale-admission")
    admission._condition = condition
    outer_closed = False

    def mutate() -> None:
        """使用复制上下文尝试执行延迟到封口后的写入。"""
        with admission.hold("延迟嵌套事务"):
            calls.append("mutated")

    try:
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="stale-admission",
        ) as executor:
            future = executor.submit(propagated.run, mutate)
            assert condition.attempted.wait(timeout=1)
            outer.__exit__(None, None, None)
            outer_closed = True
            assert admission.seal() == 0
            admission.wait_until_idle()

            condition.release.set()
            with pytest.raises(PluginMutationRejectedError):
                future.result(timeout=1)
    finally:
        condition.release.set()
        if not outer_closed:
            outer.__exit__(None, None, None)

    assert calls == []
    assert admission.active_count == 0


@pytest.mark.asyncio
async def test_quiesce_timeout_retains_admitted_owner_and_nested_reload(
    plugin_manager: PluginManager,
    monkeypatch,
) -> None:
    """停机超时保留 active owner，且封口前获准的事务可完成内部重载。"""
    manager = plugin_manager
    entered = asyncio.Event()
    release = asyncio.Event()
    manager._plugin_lifecycle.reload = MagicMock(
        return_value=PluginRuntimeStatus.ACTIVE
    )
    manager._plugin_lifecycle.quiesce_handlers = MagicMock(return_value=True)
    manager._plugin_lifecycle.finalize = MagicMock(return_value=True)

    async def mutate() -> PluginRuntimeStatus:
        """持有外层 lease，等待封口后再调用嵌套 Manager 写入口。"""
        with manager.mutation("安装插件 DemoPlugin"):
            entered.set()
            await release.wait()
            return manager.reload_plugin("DemoPlugin")

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(
            "app.runtime.extensions.plugin.manager.ThreadHelper",
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

    manager._plugin_lifecycle.reload.assert_called_once_with(
        "DemoPlugin",
        EventType.PluginReload,
    )
    assert manager._plugin_mutation_admission.active_count == 0
    assert manager.finalize_plugins() is True


def test_reload_attributes_gil_transition_to_plugin(
    plugin_manager: PluginManager,
    monkeypatch,
) -> None:
    """运行期插件加载导致 GIL 退化时应记录插件归因。"""
    states = iter((False, True))
    plugin_manager._plugin_lifecycle.reload = MagicMock(
        return_value=PluginRuntimeStatus.ACTIVE
    )
    warning = MagicMock()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin.manager.is_free_threaded_runtime",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin.manager.is_gil_enabled",
        lambda: next(states),
    )
    monkeypatch.setattr(
        "app.runtime.extensions.plugin.manager.logger.warning",
        warning,
    )

    assert plugin_manager.reload_plugin("DemoPlugin") is PluginRuntimeStatus.ACTIVE

    warning.assert_called_once()
    assert warning.call_args.args[1] == "DemoPlugin"


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
) -> None:
    """Manager 单项兼容入口在封口后返回既有失败形状且不触发底层副作用。"""
    plugin_manager._plugin_config_store = MagicMock()
    plugin_manager._plugin_instance_store = MagicMock()
    plugin_manager._plugin_sync = MagicMock()
    plugin_manager._plugin_clone = MagicMock()
    plugin_manager._plugin_mutation_admission.seal()
    plugin_manager._plugin_runtime_closed = True

    assert plugin_manager.save_plugin_config("DemoPlugin", {}) is False
    assert await plugin_manager.async_save_plugin_config("DemoPlugin", {}) is False
    assert plugin_manager.delete_plugin_config("DemoPlugin") is False
    assert plugin_manager.delete_plugin_data("DemoPlugin") is False
    assert plugin_manager.delete_plugin_instance("DemoPlugin") is False
    clone_result = plugin_manager.clone_plugin("DemoPlugin", "Work", "", "")
    assert clone_result[0] is False
    assert "停机阶段" in clone_result[1]
    with pytest.raises(PluginMutationRejectedError):
        plugin_manager.sync()

    plugin_manager._plugin_config_store.write.assert_not_called()
    plugin_manager._plugin_config_store.async_write.assert_not_called()
    plugin_manager._plugin_config_store.delete.assert_not_called()
    plugin_manager._plugin_config_store.delete_data.assert_not_called()
    plugin_manager._plugin_instance_store.delete.assert_not_called()
    plugin_manager._plugin_sync.sync.assert_not_called()
    plugin_manager._plugin_clone.clone.assert_not_called()
