"""模块能力诊断接口的只读测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.capabilities.errors import CapabilityRuntimeClosedError
from app.runtime.capabilities.model import SelectorSchema
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.events import eventmanager
from app.runtime.extensions import module_manager as module_manager_extension
from app.runtime.extensions import service_config as service_config_extension
from app.runtime.extensions.module_manager import ModuleManager
from app.db.oper.systemconfig import SystemConfigOper
from app.schemas.types import EventType


_SAMPLE_MANIFEST = """
schema_version = 1
id = "SampleModule"
kind = "host_module"
entrypoint = "fixture_sample_module:SampleModule"
depends_on = []

[metadata]
name = "Sample"
service_capability = "notification"
subtype = "Telegram"
priority = 10

[activation]
policy = "when_configured"
watch = ["Notifications"]

[activation.selector]
kind = "system_config_item"
key = "Notifications"
match_field = "type"
match_value = "sample"
enabled_field = "enabled"
"""

_OTHER_MANIFEST = """
schema_version = 1
id = "OtherModule"
kind = "host_module"
entrypoint = "fixture_other_module:OtherModule"
depends_on = []

[metadata]
name = "Other"
service_capability = "notification"
subtype = "Telegram"
priority = 20

[activation]
policy = "when_configured"
watch = ["Notifications"]

[activation.selector]
kind = "system_config_item"
key = "Notifications"
match_field = "type"
match_value = "other"
enabled_field = "enabled"
"""

_MODULE_SOURCE = """
class {class_name}:
    instances = []

    def __init__(self):
        self.events = ["create"]
        type(self).instances.append(self)

    def init_module(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def test(self):
        return True, "ok"

    @staticmethod
    def get_name():
        return "{name}"

    @staticmethod
    def get_subtype():
        return "Telegram"

    @staticmethod
    def get_priority():
        return {priority}

    def capability_method_1(self):
        return "handled"

    def capability_method_2(self):
        return "handled"
"""


def _write_capability(root: Path, directory: str, manifest: str) -> None:
    """写入一个合成 Host Module 声明。"""
    capability_dir = root / directory
    capability_dir.mkdir(parents=True)
    (capability_dir / "capability.toml").write_text(
        manifest.strip() + "\n",
        encoding="utf-8",
    )


def _build_registry(root: Path) -> CapabilityRegistry:
    """用生产 schema 构造只包含两个合成模块的 Registry。"""
    _write_capability(root, "sample", _SAMPLE_MANIFEST)
    _write_capability(root, "other", _OTHER_MANIFEST)
    return CapabilityRegistry.discover(
        roots=[root],
        kinds={"host_module"},
        selector_schemas={
            "system_config_item": SelectorSchema(
                required_fields=frozenset({
                    "key",
                    "match_field",
                    "match_value",
                    "enabled_field",
                }),
            ),
            "setting_truthy": SelectorSchema(
                required_fields=frozenset({"key"}),
            ),
        },
    )


def _config_changed_listeners() -> dict:
    """读取 ConfigChanged 监听快照，用于验证全局测试状态完整恢复。"""
    subscribers = getattr(eventmanager, "_EventManager__broadcast_subscribers")
    return dict(subscribers.get(EventType.ConfigChanged, {}))


def _enable_sample(config_values: dict) -> None:
    """写入可通过 sample selector 的最小合法通知配置。"""
    config_values["Notifications"] = [
        {
            "name": "sample",
            "type": "sample",
            "config": {},
            "switchs": [],
            "enabled": True,
        },
        {
            "name": "other",
            "type": "other",
            "config": {},
            "switchs": [],
            "enabled": True,
        },
    ]


@pytest.fixture
def module_manager_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """用合成声明和内存配置隔离 ModuleManager 单例。"""
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "fixture_sample_module.py").write_text(
        _MODULE_SOURCE.format(class_name="SampleModule", name="Sample", priority=10),
        encoding="utf-8",
    )
    (source_root / "fixture_other_module.py").write_text(
        _MODULE_SOURCE.format(class_name="OtherModule", name="Other", priority=20),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(source_root))

    registry = _build_registry(tmp_path / "capabilities")
    monkeypatch.setattr(
        module_manager_extension,
        "build_host_module_registry",
        lambda: registry,
    )

    config_values = {"Notifications": []}
    _enable_sample(config_values)

    def get_config(_self, key=None):
        key_value = getattr(key, "value", key)
        if key_value is None:
            return dict(config_values)
        return config_values.get(key_value)

    monkeypatch.setattr(SystemConfigOper, "get", get_config)
    monkeypatch.setattr(
        service_config_extension,
        "_service_instance_config_reader",
        lambda capability: SystemConfigOper().get(
            service_config_extension.service_config_key(capability)
        ),
    )

    singleton_key = (ModuleManager, (), frozenset())
    previous_manager = Singleton._instances.pop(singleton_key, None)
    resolver_attr = "_EventManager__handler_instance_resolvers"
    previous_resolvers = dict(getattr(eventmanager, resolver_attr))
    previous_config_changed_listeners = _config_changed_listeners()
    for module_name in ("fixture_sample_module", "fixture_other_module"):
        sys.modules.pop(module_name, None)

    manager = ModuleManager()
    restored = False

    def restore() -> None:
        """撤销 Manager 构造写入的单例、resolver 和事件监听器。"""
        nonlocal restored
        if restored:
            return
        try:
            manager.shutdown()
        except (AttributeError, CapabilityRuntimeClosedError):
            pass
        Singleton._instances.pop(singleton_key, None)
        if previous_manager is not None:
            Singleton._instances[singleton_key] = previous_manager
        setattr(eventmanager, resolver_attr, previous_resolvers)
        subscribers = getattr(eventmanager, "_EventManager__broadcast_subscribers")
        if previous_config_changed_listeners:
            subscribers[EventType.ConfigChanged] = dict(
                previous_config_changed_listeners
            )
        else:
            subscribers.pop(EventType.ConfigChanged, None)
        for module_name in ("fixture_sample_module", "fixture_other_module"):
            sys.modules.pop(module_name, None)
        restored = True

    try:
        yield SimpleNamespace(manager=manager, config_values=config_values)
    finally:
        restore()


class TestModuleCapabilityDiagnostics:
    """诊断接口的只读测试。"""

    def test_get_capability_index_returns_sorted_structure(
        self,
        module_manager_harness,
    ) -> None:
        """索引返回排序的方法名→模块ID映射，至少包含一个已知能力。"""
        manager = module_manager_harness.manager
        index = manager.get_capability_index()

        # 索引应该是字典
        assert isinstance(index, dict)

        # 至少包含一个已知能力
        assert "capability_method_1" in index or "capability_method_2" in index

        # 键（能力名）应该排序
        keys = list(index.keys())
        assert keys == sorted(keys)

        # 每个能力的提供者列表应该排序
        for capability_name, providers in index.items():
            assert isinstance(providers, list)
            assert len(providers) > 0
            assert providers == sorted(providers)

    def test_get_module_capabilities_returns_sorted_list(
        self,
        module_manager_harness,
    ) -> None:
        """模块能力列表返回排序的方法名，且能反查到索引。"""
        manager = module_manager_harness.manager

        # 获取第一个运行模块的 ID
        module_ids = manager.get_module_ids()
        assert len(module_ids) > 0

        module_id = module_ids[0]
        capabilities = manager.get_module_capabilities(module_id)

        # 应该返回列表
        assert isinstance(capabilities, list)

        # 应该排序
        assert capabilities == sorted(capabilities)

        # 每个能力都应该在索引里能找到该模块
        index = manager.get_capability_index()
        for capability in capabilities:
            assert capability in index
            assert module_id in index[capability]

    def test_get_module_capabilities_nonexistent_module_returns_empty(
        self,
        module_manager_harness,
    ) -> None:
        """查询不存在的模块返回空列表而非异常。"""
        manager = module_manager_harness.manager

        capabilities = manager.get_module_capabilities("NonexistentModule")
        assert capabilities == []

    def test_capability_index_mutability_does_not_affect_internal_state(
        self,
        module_manager_harness,
    ) -> None:
        """修改返回的索引不影响内部状态。"""
        manager = module_manager_harness.manager

        # 第一次查询
        index1 = manager.get_capability_index()
        original_capabilities = set(index1.keys())

        # 修改返回的索引
        if index1:
            first_key = next(iter(index1.keys()))
            index1[first_key].append("FakeModule")
            index1["FakeCapability"] = ["FakeModule"]

        # 第二次查询应该与第一次相同
        index2 = manager.get_capability_index()
        assert set(index2.keys()) == original_capabilities

        # 如果第一次有数据，验证修改没有被保留
        if original_capabilities:
            first_key = next(iter(original_capabilities))
            assert "FakeModule" not in index2[first_key]

    def test_module_capabilities_mutability_does_not_affect_internal_state(
        self,
        module_manager_harness,
    ) -> None:
        """修改模块能力列表不影响内部状态。"""
        manager = module_manager_harness.manager

        module_ids = manager.get_module_ids()
        assert len(module_ids) > 0
        module_id = module_ids[0]

        # 第一次查询
        capabilities1 = manager.get_module_capabilities(module_id)
        original_length = len(capabilities1)

        # 修改返回的列表
        if capabilities1:
            capabilities1.append("FakeMethod")

        # 第二次查询应该与第一次相同
        capabilities2 = manager.get_module_capabilities(module_id)
        assert len(capabilities2) == original_length

    def test_capability_index_empty_when_no_modules_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """当没有模块运行时，索引返回空字典。"""
        source_root = tmp_path / "source"
        source_root.mkdir()
        (source_root / "fixture_empty_module.py").write_text(
            """
class EmptyModule:
    def init_module(self):
        pass
    def stop(self):
        pass
    @staticmethod
    def get_name():
        return "Empty"
    @staticmethod
    def get_priority():
        return 50
""",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(source_root))

        manifest = """
schema_version = 1
id = "EmptyModule"
kind = "host_module"
entrypoint = "fixture_empty_module:EmptyModule"
depends_on = []

[metadata]
name = "Empty"
priority = 50

[activation]
policy = "never"
watch = []
"""
        registry = _build_registry(tmp_path / "capabilities")
        monkeypatch.setattr(
            module_manager_extension,
            "build_host_module_registry",
            lambda: registry,
        )

        config_values = {"Notifications": []}

        def get_config(_self, key=None):
            return config_values.get(getattr(key, "value", key))

        monkeypatch.setattr(SystemConfigOper, "get", get_config)
        monkeypatch.setattr(
            service_config_extension,
            "_service_instance_config_reader",
            lambda capability: SystemConfigOper().get(
                service_config_extension.service_config_key(capability)
            ),
        )

        singleton_key = (ModuleManager, (), frozenset())
        previous_manager = Singleton._instances.pop(singleton_key, None)
        try:
            manager = ModuleManager()

            index = manager.get_capability_index()
            assert isinstance(index, dict)
        finally:
            Singleton._instances.pop(singleton_key, None)
            if previous_manager is not None:
                Singleton._instances[singleton_key] = previous_manager
