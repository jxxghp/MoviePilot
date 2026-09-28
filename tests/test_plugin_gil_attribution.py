"""free-threaded 运行时插件触发 GIL 回退的归因测试。"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.runtime.extensions.plugin import gil as gil_module
from app.runtime.extensions.plugin import loader as loader_module
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.gil import attribute_gil_fallback
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.registry import PluginRegistry
from app.schemas.plugin import PluginRuntimeStatus


class _GilState:
    """模拟进程级 GIL 状态：一旦开启就保持到进程结束。"""

    def __init__(self) -> None:
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True


@pytest.fixture
def gil_state(monkeypatch) -> _GilState:
    """在 free-threaded 运行时下用可控的 GIL 状态替换真实解释器状态。"""
    state = _GilState()
    monkeypatch.setattr(gil_module, "is_free_threaded_runtime", lambda: True)
    monkeypatch.setattr(gil_module, "is_gil_enabled", lambda: state.enabled)
    return state


def test_attribution_records_only_transition(gil_state):
    """只有区间内发生关闭到开启的转换才归因，GIL 已开启时后续插件不再被记录。"""
    recorded: list[str] = []

    with attribute_gil_fallback("Quiet", recorded.append):
        pass
    with attribute_gil_fallback("Native", recorded.append):
        gil_state.enable()
    with attribute_gil_fallback("Later", recorded.append):
        pass

    assert recorded == ["Native"]


def test_attribution_records_even_when_load_fails(gil_state):
    """导入原生扩展后插件自身抛错，回退事实仍要归因，异常照常外抛。"""
    recorded: list[str] = []

    with pytest.raises(RuntimeError):
        with attribute_gil_fallback("Broken", recorded.append):
            gil_state.enable()
            raise RuntimeError("init failed")

    assert recorded == ["Broken"]


def test_standard_runtime_never_attributes(monkeypatch):
    """标准解释器 GIL 本来就开启，不存在回退，不做归因。"""
    monkeypatch.setattr(gil_module, "is_free_threaded_runtime", lambda: False)
    states = iter((False, True))
    monkeypatch.setattr(gil_module, "is_gil_enabled", lambda: next(states))
    recorded: list[str] = []

    with attribute_gil_fallback("Demo", recorded.append):
        pass

    assert recorded == []


def test_registry_tracks_gil_fallback_with_generation():
    """注册表只在首次记录时推进刷新代次，移除与清空会同步清掉归因。"""
    registry = PluginRegistry()
    start = registry.generation

    registry.mark_gil_fallback("Native")
    registry.mark_gil_fallback("Native")
    assert registry.gil_fallback_snapshot() == ["Native"]
    assert registry.generation == start + 1

    registry.remove("Native")
    assert registry.gil_fallback_snapshot() == []
    assert registry.generation == start + 2

    registry.mark_gil_fallback("Other")
    registry.clear()
    assert registry.gil_fallback_snapshot() == []


def test_loader_attributes_module_import_to_installed_plugin_id(tmp_path, monkeypatch, gil_state):
    """全量加载时逐个模块观察，回退归因到导入原生扩展的插件，并还原安装时的 ID 大小写。"""
    for name in ("pureplugin", "nativeplugin"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")

    def import_module(module_name: str) -> ModuleType:
        if module_name == "app.plugins.nativeplugin":
            gil_state.enable()
        return ModuleType(module_name)

    monkeypatch.setattr(loader_module, "importlib", SimpleNamespace(import_module=import_module))
    recorded: list[str] = []
    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=MagicMock(),
        gil_fallback_recorder=recorded.append,
    )

    loader.load(None, ["PurePlugin", "NativePlugin"], lambda _candidate: True)

    assert recorded == ["NativePlugin"]


def test_lifecycle_attributes_init_plugin_import(gil_state):
    """插件在 init_plugin 中才导入原生扩展时，回退归因到该插件。"""

    class LazyNative:
        plugin_name = "惰性原生"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            gil_state.enable()

        @staticmethod
        def get_state():
            return True

    recorded: list[str] = []
    lifecycle = PluginLifecycle(
        classes={},
        running={},
        load_plugins=lambda _plugin_id, _installed, _check: [LazyNative],
        loadable_plugins=lambda: ["LazyNative"],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: True,
        clear_modules=MagicMock(),
        clear_tools=MagicMock(),
        enable_events=MagicMock(),
        disable_events=MagicMock(),
        runtime_status_writer=lambda _plugin_id, _status: None,
        runtime_compatible=lambda _plugin_id: True,
        database=lambda: PluginDatabase(),
        log=MagicMock(),
        event_sender=MagicMock(),
        gil_fallback_recorder=recorded.append,
    )

    assert lifecycle.start() == {"LazyNative": PluginRuntimeStatus.ACTIVE}
    assert recorded == ["LazyNative"]
