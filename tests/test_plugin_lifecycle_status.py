"""插件加载六类状态中的运行结果测试。"""

from types import SimpleNamespace
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.instance import instance_key
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus


class DemoPlugin:
    """满足插件最小生命周期合同的测试类。"""

    plugin_name = "演示插件"
    plugin_version = "1.0.0"
    plugin_order = 0

    def init_plugin(self, _config=None):
        """占位实现，实例启动由用例替身接管。"""

    @staticmethod
    def get_state():
        """报告实例处于启用状态。"""
        return True


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _prepare(monkeypatch, manager: PluginManager, *, plugins, auth=True, start=None):
    """隔离插件目录扫描、权限判定和实例启动，只保留状态判定路径。"""
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_storage",
        lambda: SimpleNamespace(read=lambda _key: ["DemoPlugin"]),
    )
    monkeypatch.setattr(
        manager, "_load_selective_plugins", lambda *_args, **_kwargs: list(plugins)
    )
    monkeypatch.setattr(manager, "_plugin_instance_ids", lambda _plugin_id: ["default"])
    monkeypatch.setattr(
        manager,
        "_PluginManager__set_and_check_auth_level",
        lambda **_kwargs: auth,
    )
    monkeypatch.setattr(manager, "_sync_family_event_state", lambda *_args: None)

    def default_start(_plugin_class, plugin_id, instance_id):
        """把实例登记为运行态，模拟一次成功启动。"""
        manager._running_plugins[plugin_id] = object()
        return True

    monkeypatch.setattr(
        manager, "_start_instance_with_version", start or default_start
    )


def test_start_records_active_result(monkeypatch, plugin_manager: PluginManager):
    """插件完成构造和初始化后进入 active。"""
    _prepare(monkeypatch, plugin_manager, plugins=[DemoPlugin])

    result = plugin_manager.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.ACTIVE}
    assert "DemoPlugin" in plugin_manager.plugins
    assert "DemoPlugin" in plugin_manager.running_plugins
    assert plugin_manager.get_plugin_runtime_statuses()["DemoPlugin"] is (
        PluginRuntimeStatus.ACTIVE
    )


def test_start_records_policy_block_without_runtime_instance(
    monkeypatch, plugin_manager: PluginManager
):
    """类已发现但权限策略拒绝时进入 blocked_by_policy。"""
    _prepare(monkeypatch, plugin_manager, plugins=[DemoPlugin], auth=False)

    result = plugin_manager.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.BLOCKED_BY_POLICY}
    assert plugin_manager.running_plugins == {}
    assert plugin_manager.get_plugin_runtime_statuses()["DemoPlugin"] is (
        PluginRuntimeStatus.BLOCKED_BY_POLICY
    )


def test_start_records_load_failure_for_init_exception(
    monkeypatch, plugin_manager: PluginManager
):
    """插件初始化异常时保留类信息并进入 load_failed。"""

    def raise_on_start(*_args, **_kwargs):
        """模拟插件自身代码在实例化阶段抛出异常。"""
        raise RuntimeError("init failed")

    _prepare(
        monkeypatch, plugin_manager, plugins=[DemoPlugin], start=raise_on_start
    )

    result = plugin_manager.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert "DemoPlugin" in plugin_manager.plugins
    assert plugin_manager.running_plugins == {}


def test_start_records_load_failure_when_no_instance_enters_runtime(
    monkeypatch, plugin_manager: PluginManager
):
    """全部实例都没能进入运行态时按加载失败记录，不谎报激活。"""
    _prepare(
        monkeypatch,
        plugin_manager,
        plugins=[DemoPlugin],
        start=lambda *_args, **_kwargs: False,
    )

    result = plugin_manager.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert plugin_manager.running_plugins == {}


def test_start_records_load_failure_when_loader_returns_no_class(
    monkeypatch, plugin_manager: PluginManager
):
    """目标源码无法产生合法插件类时进入 load_failed。"""
    _prepare(monkeypatch, plugin_manager, plugins=[])

    result = plugin_manager.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert plugin_manager.running_plugins == {}


def test_start_keeps_family_active_when_one_instance_fails(
    monkeypatch, plugin_manager: PluginManager
):
    """一个实例启动失败不改变整族状态，兄弟实例仍在运行即为激活。"""
    started = []

    def start_second_only(_plugin_class, plugin_id, instance_id):
        """只放行第二个实例，第一个按失败返回。"""
        started.append(instance_id)
        if instance_id == "broken":
            return False
        plugin_manager._running_plugins[instance_key(plugin_id, instance_id)] = object()
        return True

    _prepare(monkeypatch, plugin_manager, plugins=[DemoPlugin], start=start_second_only)
    monkeypatch.setattr(
        plugin_manager, "_plugin_instance_ids", lambda _plugin_id: ["broken", "healthy"]
    )

    result = plugin_manager.start("DemoPlugin")

    assert started == ["broken", "healthy"]
    assert result == {"DemoPlugin": PluginRuntimeStatus.ACTIVE}


def test_reload_returns_latest_runtime_status(
    monkeypatch, plugin_manager: PluginManager
):
    """热重载返回本次加载结果，供接口区分成功、权限拒绝和加载失败。"""
    _prepare(monkeypatch, plugin_manager, plugins=[DemoPlugin], auth=False)
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.eventmanager",
        SimpleNamespace(
            send_event=lambda *_args, **_kwargs: None,
            disable_event_handler=lambda *_args, **_kwargs: None,
            enable_event_handler=lambda *_args, **_kwargs: None,
            register_handler_instance_resolver=lambda *_args, **_kwargs: None,
        ),
    )

    assert plugin_manager.reload_plugin("DemoPlugin") is (
        PluginRuntimeStatus.BLOCKED_BY_POLICY
    )
