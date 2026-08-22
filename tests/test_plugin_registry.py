from types import SimpleNamespace

from app.runtime.extensions.registry.plugin import PluginRegistry
from app.schemas.plugin import PluginRuntimeStatus


def test_registry_owns_classes_instances_and_stable_snapshots():
    """注册表集中持有类与实例，快照不受后续热重载修改影响。"""
    registry = PluginRegistry()
    plugin_class = type("Demo", (), {})
    plugin_instance = SimpleNamespace(plugin_name="演示")
    registry.classes["Demo"] = plugin_class
    registry.running["Demo"] = plugin_instance

    snapshot = registry.running_snapshot()
    registry.running["Other"] = SimpleNamespace(plugin_name="其它")

    assert registry.has_class("Demo")
    assert registry.plugin_class("Demo") is plugin_class
    assert registry.instance("Demo") is plugin_instance
    assert registry.plugin_ids() == ["Demo"]
    assert registry.running_ids() == ["Demo", "Other"]
    assert list(snapshot) == ["Demo"]


def test_registry_clear_preserves_compatibility_mapping_identity():
    """整体停止插件时原地清空，旧调用方持有的字典引用继续有效。"""
    registry = PluginRegistry()
    classes = registry.classes
    running = registry.running
    classes["Demo"] = object()
    running["Demo"] = object()

    registry.clear()

    assert registry.classes is classes
    assert registry.running is running
    assert classes == {}
    assert running == {}


def test_registry_tracks_runtime_status_generation_and_settling():
    """状态与后台收敛变化只在真实改变时推进刷新代次。"""
    registry = PluginRegistry()

    registry.set_runtime_status("Demo", PluginRuntimeStatus.READY)
    first_generation = registry.generation
    registry.set_runtime_status("Demo", PluginRuntimeStatus.READY)
    registry.set_settling(True)

    assert registry.runtime_status("Demo") is PluginRuntimeStatus.READY
    assert registry.runtime_status_snapshot() == {
        "Demo": PluginRuntimeStatus.READY,
    }
    assert registry.generation == first_generation + 1
    assert registry.settling is True

    registry.remove("Demo")

    assert registry.runtime_status("Demo") is None
