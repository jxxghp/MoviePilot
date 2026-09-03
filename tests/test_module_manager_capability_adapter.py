"""Host Module Adapter 对 Capability Runtime 的兼容合同测试。"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import Mock

import pytest

from app.db.oper.systemconfig import SystemConfigOper
from app.foundation.singleton import Singleton
from app.runtime.capabilities.errors import CapabilityRuntimeClosedError
from app.runtime.capabilities.model import CapabilityLifecycleState, SelectorSchema
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.events import Event, EventHandlerBinding, eventmanager
from app.runtime.extensions.module import manager as module_manager_extension
from app.runtime.extensions.module.manager import ModuleManager
from app.runtime.extensions.service import configure_service_config_reader
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType

_SAMPLE_MANIFEST = """
schema_version = 1
id = "SampleModule"
kind = "host_module"
entrypoint = "fixture_sample_module:SampleModule"
depends_on = []

[metadata]
name = "Sample"
type = "notification"
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
type = "notification"
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

    def capability_method(self):
        return "handled"

    @staticmethod
    def get_name():
        return "{name}"

    @staticmethod
    def get_type():
        return "notification"

    @staticmethod
    def get_subtype():
        return "Telegram"

    @staticmethod
    def get_priority():
        return {priority}
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


def _run_real_host_module_check(tmp_path: Path, body: str) -> None:
    """在隔离后端和进程内网络守卫下执行真实 Host Module 合同检查。"""
    project_root = Path(__file__).parents[1]
    prelude = r"""
import ipaddress
import socket
import sys

network_attempts = []
allowed_hosts = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::", ""}
real_getaddrinfo = socket.getaddrinfo
real_connect = socket.socket.connect

def is_allowed_host(host):
    normalized = host.decode() if isinstance(host, (bytes, bytearray)) else host
    if normalized is None or normalized in allowed_hosts:
        return True
    try:
        address = ipaddress.ip_address(str(normalized).split("%", 1)[0])
        return address.is_loopback or address.is_unspecified
    except ValueError:
        return False

def block_network(operation, host):
    network_attempts.append((operation, host))
    raise AssertionError(f"Host Module 合同测试禁止真实出站：{operation} {host!r}")

def guarded_getaddrinfo(host, *args, **kwargs):
    if not is_allowed_host(host):
        block_network("DNS", host)
    return real_getaddrinfo(host, *args, **kwargs)

def guarded_connect(sock, address):
    if isinstance(address, tuple) and address and not is_allowed_host(address[0]):
        block_network("socket", address[0])
    return real_connect(sock, address)

socket.getaddrinfo = guarded_getaddrinfo
socket.socket.connect = guarded_connect

from app.testing.bootstrap import prepare_backend
prepare_backend()
"""
    code = f"{prelude}\n{body}\nassert network_attempts == [], network_attempts\n"
    env = os.environ.copy()
    env["CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        f"真实 Host Module 合同检查失败：\nstdout:\n{result.stdout[-4000:]}\n"
        f"stderr:\n{result.stderr[-8000:]}"
    )


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

    def get_config(_self, key=None):
        key_value = getattr(key, "value", key)
        if key_value is None:
            return dict(config_values)
        return config_values.get(key_value)

    monkeypatch.setattr(SystemConfigOper, "get", get_config)
    previous_config_reader = configure_service_config_reader(
        lambda key: SystemConfigOper().get(key)
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
        configure_service_config_reader(previous_config_reader)
        restored = True

    try:
        yield SimpleNamespace(
            manager=manager,
            config_values=config_values,
            previous_config_changed_listeners=previous_config_changed_listeners,
            restore=restore,
        )
    finally:
        restore()


def _enable_sample(config_values: dict) -> None:
    """写入可通过 sample selector 的最小合法通知配置。"""
    config_values["Notifications"] = [
        {
            "name": "sample",
            "type": "sample",
            "config": {},
            "switchs": [],
            "enabled": True,
        }
    ]


def test_harness_restores_module_manager_config_listener(
    module_manager_harness,
) -> None:
    """Fixture teardown 不能把临时 Manager 的 bound listener 留在全局事件总线。"""
    manager = module_manager_harness.manager
    current_listeners = _config_changed_listeners()

    assert current_listeners != (
        module_manager_harness.previous_config_changed_listeners
    )
    assert any(
        getattr(listener, "__self__", None) is manager
        for listener in current_listeners.values()
    )

    module_manager_harness.restore()

    assert _config_changed_listeners() == (
        module_manager_harness.previous_config_changed_listeners
    )


def test_specs_are_lightweight_and_do_not_materialize_modules(
    module_manager_harness,
) -> None:
    """ModuleManager 的声明视图不能解析任何 Host Module 实现。"""
    manager = module_manager_harness.manager

    specs = manager.list_specs()

    assert manager.get_specs() == specs
    assert [spec.id for spec in specs] == ["OtherModule", "SampleModule"]
    assert [spec.metadata["name"] for spec in specs] == ["Other", "Sample"]
    assert "fixture_sample_module" not in sys.modules
    assert "fixture_other_module" not in sys.modules
    assert manager.get_running_module("SampleModule") is None


def test_get_module_materializes_one_canonical_class_without_starting_it(
    module_manager_harness,
) -> None:
    """兼容查询返回 canonical class，但不创建或启动资源。"""
    manager = module_manager_harness.manager

    module_class = manager.get_module("SampleModule")
    canonical_class = importlib.import_module(
        "fixture_sample_module"
    ).SampleModule

    assert module_class is canonical_class
    assert manager.get_module("SampleModule") is canonical_class
    assert canonical_class.instances == []
    assert manager.get_running_module("SampleModule") is None
    assert "fixture_other_module" not in sys.modules


def test_get_modules_materializes_all_real_classes_without_starting_them(
    module_manager_harness,
) -> None:
    """旧 get_modules 合同保留真实 class 字典，不返回代理或隐式激活。"""
    manager = module_manager_harness.manager

    modules = manager.get_modules()

    sample_module = importlib.import_module("fixture_sample_module")
    other_module = importlib.import_module("fixture_other_module")
    assert modules == {
        "OtherModule": other_module.OtherModule,
        "SampleModule": sample_module.SampleModule,
    }
    assert sample_module.SampleModule.instances == []
    assert other_module.OtherModule.instances == []
    assert manager.get_running_module("SampleModule") is None
    assert manager.get_running_module("OtherModule") is None


def test_config_reconcile_reload_and_stop_preserve_manager_contract(
    module_manager_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置激活、全量 reload 与可重启 stop 保持同步可观察顺序。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)

    manager.load_modules()
    first = manager.get_running_module("SampleModule")
    assert first is not None
    assert first.events == ["create", "start"]

    send_event = Mock()
    monkeypatch.setattr(eventmanager, "send_event", send_event)
    manager.reload()
    second = manager.get_running_module("SampleModule")

    assert second is not None
    assert second is not first
    assert first.events == ["create", "start", "stop"]
    assert second.events == ["create", "start"]
    send_event.assert_called_once_with(etype=EventType.ModuleReload, data={})

    manager.stop()
    assert manager.get_running_module("SampleModule") is None
    assert second.events == ["create", "start", "stop"]

    manager.load_modules()
    restarted = manager.get_running_module("SampleModule")
    assert restarted is not None
    assert restarted is not second
    assert restarted.events == ["create", "start"]

    module_manager_harness.config_values["Notifications"] = []
    manager.load_modules()
    assert manager.get_running_module("SampleModule") is None
    assert restarted.events == ["create", "start", "stop"]


def test_module_manager_lifecycle_keeps_monotonic_transition_generations(
    module_manager_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manager 的旧同步入口必须按共享状态机顺序提交每一代转换。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)
    monkeypatch.setattr(eventmanager, "send_event", Mock())

    manager.load_modules()
    manager.reload()
    manager.stop()
    manager.load_modules()
    module_manager_harness.config_values["Notifications"] = []
    manager.load_modules()

    observations = manager._runtime.observations("SampleModule")
    assert [
        (item.generation, item.operation, item.outcome)
        for item in observations
    ] == [
        (1, "activate", "started"),
        (1, "activate", "succeeded"),
        (2, "stop", "started"),
        (2, "stop", "succeeded"),
        (3, "activate", "started"),
        (3, "activate", "succeeded"),
        (4, "stop", "started"),
        (4, "stop", "succeeded"),
        (5, "activate", "started"),
        (5, "activate", "succeeded"),
        (6, "stop", "started"),
        (6, "stop", "succeeded"),
    ]
    snapshot = manager._runtime.snapshot("SampleModule")
    assert snapshot.generation == 6
    assert snapshot.lifecycle is CapabilityLifecycleState.STOPPED
    assert snapshot.visible is False


def test_config_event_reloads_same_instance_and_tracks_selector_changes(
    module_manager_harness,
) -> None:
    """配置事件由 Host Adapter 唯一协调，并保留模块实例内的重载状态。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)
    manager.load_modules()
    running = manager.get_running_module("SampleModule")

    manager.handle_config_changed(
        Event(
            EventType.ConfigChanged,
            ConfigChangeEventData(key="Notifications"),
        )
    )

    assert manager.get_running_module("SampleModule") is running
    assert running.events == ["create", "start", "stop", "start"]

    module_manager_harness.config_values["Notifications"] = []
    manager.handle_config_changed(
        Event(
            EventType.ConfigChanged,
            ConfigChangeEventData(key="Notifications"),
        )
    )
    assert manager.get_running_module("SampleModule") is None
    assert running.events == ["create", "start", "stop", "start", "stop"]


def test_registered_config_event_activates_newly_enabled_module(
    module_manager_harness,
) -> None:
    """事件总线必须把配置变更绑定回当前 ModuleManager 实例。"""
    manager = module_manager_harness.manager
    handler = next(
        listener
        for listener in _config_changed_listeners().values()
        if getattr(listener, "__self__", None) is manager
    )
    _enable_sample(module_manager_harness.config_values)

    eventmanager._EventManager__invoke_handler_by_type_sync(
        handler,
        Event(
            EventType.ConfigChanged,
            ConfigChangeEventData(key="Notifications"),
        ),
    )

    running = manager.get_running_module("SampleModule")
    assert running is not None
    assert running.events == ["create", "start"]


def test_shutdown_is_irreversible(module_manager_harness, monkeypatch) -> None:
    """shutdown 撤销全部可见实例，并拒绝通过 load_modules 再次启动。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)
    manager.load_modules()
    running = manager.get_running_module("SampleModule")
    assert running is not None
    remove_listener = Mock()
    unregister_resolver = Mock()
    monkeypatch.setattr(eventmanager, "remove_event_listener", remove_listener)
    monkeypatch.setattr(
        eventmanager,
        "unregister_handler_instance_resolver",
        unregister_resolver,
    )

    manager.shutdown()

    assert manager.get_running_module("SampleModule") is None
    assert running.events == ["create", "start", "stop"]
    manager.load_modules()
    assert manager.get_running_module("SampleModule") is None
    assert type(running).instances == [running]
    remove_listener.assert_called_once_with(
        EventType.ConfigChanged,
        manager.handle_config_changed,
    )
    unregister_resolver.assert_called_once_with("modules")


def test_shutdown_reports_unreleased_module_owner(module_manager_harness) -> None:
    """Host Module 返回 False 时 Runtime 必须保留 owner 并向组合根报告。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)
    manager.load_modules()
    running = manager.get_running_module("SampleModule")
    running.stop = Mock(side_effect=[False, None])

    assert manager.shutdown() is False
    failed = manager._runtime.snapshot("SampleModule")
    assert failed.lifecycle is CapabilityLifecycleState.FAILED
    assert failed.visible is False

    assert manager.shutdown() is True
    assert running.stop.call_count == 2


def test_all_real_host_modules_zero_arg_construct_without_starting_resources(
    tmp_path: Path,
) -> None:
    """每份真实 manifest 都必须能解析 canonical class 并零参数构造且不启动资源。"""
    body = r"""
from app.runtime.extensions.module.adapter import (
    HostModuleAdapter,
    build_host_module_registry,
)

registry = build_host_module_registry()
specs = registry.list_specs()
assert len(specs) == 40

adapter = HostModuleAdapter()
lifecycle_events = []
instances = {}

def make_recorder(operation, capability_id):
    def record(instance):
        lifecycle_events.append((operation, capability_id, id(instance)))
    return record

for spec in specs:
    implementation = adapter.materialize(spec)
    module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
    assert implementation is getattr(sys.modules[module_name], symbol_name)
    implementation.init_module = make_recorder("start", spec.id)
    implementation.stop = make_recorder("stop", spec.id)

    instance = adapter.create(spec, implementation, generation=1)
    assert type(instance) is implementation
    instances[spec.id] = instance

assert set(instances) == {spec.id for spec in specs}
assert lifecycle_events == []
"""
    _run_real_host_module_check(tmp_path, body)


def test_real_manifest_inventory_drives_full_module_manager_lifecycle(
    tmp_path: Path,
) -> None:
    """真实声明自动驱动全量激活、原实例重载、禁用停止和不可逆关闭门禁。"""
    body = r"""
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.capabilities.model import ActivationPolicy
from app.runtime.config import settings
from app.runtime.events import Event
from app.runtime.extensions.module.adapter import (
    HostModuleAdapter,
    build_host_module_registry,
)
from app.runtime.extensions.service import configure_service_config_reader
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType

registry = build_host_module_registry()
specs = registry.list_specs()
assert len(specs) == 40
spec_by_id = {spec.id: spec for spec in specs}

events = {spec.id: [] for spec in specs}
adapter = HostModuleAdapter()

def make_recorder(operation, capability_id):
    def record(instance):
        events[capability_id].append((operation, id(instance)))
    return record

for spec in specs:
    implementation = adapter.materialize(spec)
    implementation.init_module = make_recorder("start", spec.id)
    implementation.stop = make_recorder("stop", spec.id)

config_values = {}
enabled_service_values = {}
selector_keys = set()
configured_ids = set()
for spec in specs:
    if spec.activation is not ActivationPolicy.WHEN_CONFIGURED:
        continue
    configured_ids.add(spec.id)
    selector = spec.selector
    assert selector is not None
    key = str(selector.config["key"])
    selector_keys.add(key)
    if selector.kind == "setting_truthy":
        setattr(settings, key, f"enabled:{spec.id}")
    elif selector.kind == "system_config_item":
        enabled_service_values.setdefault(key, []).append({
            "name": f"contract-{spec.id}",
            "type": selector.config["match_value"],
            "config": {},
            "enabled": True,
        })
    else:
        raise AssertionError(f"未覆盖的 Host Module selector：{selector.kind}")
config_values.update({key: list(value) for key, value in enabled_service_values.items()})

def get_config(_self, key=None):
    key_value = getattr(key, "value", key)
    if key_value is None:
        return dict(config_values)
    return config_values.get(key_value)

SystemConfigOper.get = get_config
configure_service_config_reader(lambda key: SystemConfigOper().get(key))

from app.runtime.extensions.module.manager import ModuleManager

manager = ModuleManager()
bootstrap_ids = {
    spec.id
    for spec in specs
    if spec.activation is ActivationPolicy.BOOTSTRAP
}
initial_ids = bootstrap_ids | configured_ids
assert bootstrap_ids
assert configured_ids
assert initial_ids == {spec.id for spec in specs}
initial_instances = {
    capability_id: manager.get_running_module(capability_id)
    for capability_id in initial_ids
}
assert all(initial_instances.values())
assert {
    capability_id: [operation for operation, _instance_id in events[capability_id]]
    for capability_id in initial_ids
} == {capability_id: ["start"] for capability_id in initial_ids}

watch_keys = {key for spec in specs for key in spec.watch}
watched_ids = {
    spec.id
    for spec in specs
    if spec.id in initial_ids and watch_keys.intersection(spec.watch)
}
manager.handle_config_changed(
    Event(
        EventType.ConfigChanged,
        ConfigChangeEventData(key=watch_keys),
    )
)

for capability_id, initial_instance in initial_instances.items():
    assert manager.get_running_module(capability_id) is initial_instance
    operations = [operation for operation, _instance_id in events[capability_id]]
    expected = ["start", "stop", "start"] if capability_id in watched_ids else ["start"]
    assert operations == expected, (capability_id, operations)
    assert {
        instance_id for _operation, instance_id in events[capability_id]
    } == {id(initial_instance)}

for spec in specs:
    if spec.id not in configured_ids:
        continue
    selector = spec.selector
    key = str(selector.config["key"])
    if selector.kind == "setting_truthy":
        setattr(settings, key, False)
for key in enabled_service_values:
    config_values[key] = []

manager.handle_config_changed(
    Event(
        EventType.ConfigChanged,
        ConfigChangeEventData(key=selector_keys),
    )
)
for capability_id in configured_ids:
    assert manager.get_running_module(capability_id) is None
    assert [operation for operation, _instance_id in events[capability_id]] == [
        "start",
        "stop",
        "start",
        "stop",
    ]
for capability_id in bootstrap_ids:
    assert manager.get_running_module(capability_id) is initial_instances[capability_id]

manager.shutdown()
assert all(manager.get_running_module(spec.id) is None for spec in specs)
events_after_shutdown = {
    capability_id: list(capability_events)
    for capability_id, capability_events in events.items()
}

for spec in specs:
    if spec.id not in configured_ids:
        continue
    selector = spec.selector
    key = str(selector.config["key"])
    if selector.kind == "setting_truthy":
        setattr(settings, key, f"re-enabled:{spec.id}")
for key, value in enabled_service_values.items():
    config_values[key] = list(value)

manager.load_modules()
manager.handle_config_changed(
    Event(
        EventType.ConfigChanged,
        ConfigChangeEventData(key=watch_keys),
    )
)
assert all(manager.get_running_module(spec.id) is None for spec in specs)
assert events == events_after_shutdown
assert set(spec_by_id) == set(events)
"""
    _run_real_host_module_check(tmp_path, body)


def test_default_config_keeps_every_manifest_configured_entrypoint_unimported(
    tmp_path: Path,
) -> None:
    """默认配置惰性边界由全部 when-configured manifest 自动生成。"""
    body = r"""
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.capabilities.model import ActivationPolicy
from app.runtime.config import settings
from app.runtime.extensions.module.adapter import (
    HostModuleAdapter,
    build_host_module_registry,
)

registry = build_host_module_registry()
specs = registry.list_specs()
assert len(specs) == 40
configured_specs = tuple(
    spec for spec in specs
    if spec.activation is ActivationPolicy.WHEN_CONFIGURED
)
configured_modules = {
    spec.entrypoint.split(":", maxsplit=1)[0]
    for spec in configured_specs
}
assert configured_modules
assert configured_modules.isdisjoint(sys.modules)

for spec in configured_specs:
    selector = spec.selector
    assert selector is not None
    if selector.kind == "setting_truthy":
        setattr(settings, str(selector.config["key"]), False)

SystemConfigOper.get = lambda _self, key=None: {} if key is None else []

adapter = HostModuleAdapter()
for spec in specs:
    if spec.activation is not ActivationPolicy.BOOTSTRAP:
        continue
    implementation = adapter.materialize(spec)
    implementation.init_module = lambda _self: None
    implementation.stop = lambda _self: None

assert configured_modules.isdisjoint(sys.modules)

from app.runtime.extensions.module.manager import ModuleManager

manager = ModuleManager()
assert manager.get_specs() == manager.list_specs()
assert {spec.id for spec in manager.list_specs()} == {spec.id for spec in specs}
assert all(manager.get_running_module(spec.id) is None for spec in configured_specs)
assert configured_modules.isdisjoint(sys.modules)
manager.shutdown()
assert configured_modules.isdisjoint(sys.modules)
"""
    _run_real_host_module_check(tmp_path, body)


def test_event_resolver_uses_exact_class_and_blocks_stopped_owner_fallback(
    module_manager_harness,
) -> None:
    """同名 class 不能冒充 owner；已停止 owner 必须返回 Binding(None)。"""
    manager = module_manager_harness.manager
    _enable_sample(module_manager_harness.config_values)
    manager.load_modules()
    module_class = manager.get_module("SampleModule")
    running = manager.get_running_module("SampleModule")

    active_binding = manager.resolve_event_handler_instance(module_class)
    assert active_binding == EventHandlerBinding(
        instance=running,
        owner_name="Sample",
    )

    impostor = type("SampleModule", (), {})
    impostor.__module__ = module_class.__module__
    assert manager.resolve_event_handler_instance(impostor) is None

    manager.stop()
    stopped_binding = manager.resolve_event_handler_instance(module_class)
    assert stopped_binding == EventHandlerBinding(
        instance=None,
        owner_name="Sample",
    )


def test_default_modulelist_does_not_import_unconfigured_provider_sdks(
    tmp_path: Path,
) -> None:
    """默认配置下构造 Manager 和查询模块列表都不能拉起重量 provider SDK。"""
    project_root = Path(__file__).parents[1]
    code = """
from app.testing.bootstrap import prepare_backend
prepare_backend()

from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.config import settings

def empty_config(self, key=None):
    return {} if key is None else []

SystemConfigOper.get = empty_config
settings.ACOUSTID_API_KEY = None
settings.FANART_API_KEY = None

from app.runtime.extensions.module.manager import ModuleManager
from app.application.module import configure_module_runtime

configure_module_runtime(lambda: ModuleManager())

manager = ModuleManager()
assert len(manager.list_specs()) == 40
assert manager.get_specs() == manager.list_specs()

from app.api.endpoints.system import modulelist
response = modulelist(None)
assert len(response.data["modules"]) == 40

heavy_prefixes = (
    "lark_oapi",
    "slack_bolt",
    "slack_sdk",
    "discord",
    "plexapi",
    "telebot",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in heavy_prefixes)
)
assert loaded == [], loaded
manager.shutdown()
"""
    env = os.environ.copy()
    env["CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", "import sys\n" + code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        f"子进程模块发现失败：\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )


def test_lazy_boundary_annotations_are_reflectable_without_provider_sdks(
    tmp_path: Path,
) -> None:
    """宿主公共注解可被反射，且反射过程不加载可选 provider SDK。"""
    project_root = Path(__file__).parents[1]
    code = """
from app.testing.bootstrap import prepare_backend
prepare_backend()

import sys
from typing import List, Optional, get_type_hints

provider_prefixes = ("qbittorrentapi", "transmission_rpc", "pywebpush")

def loaded_provider_modules():
    return sorted(
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in provider_prefixes
        )
    )

assert loaded_provider_modules() == []

from app.chain.base import ChainBase
from app.api.endpoints.message import WebPushError, is_webpush_subscription_gone
from app.schemas.transfer import DownloaderFile

assert get_type_hints(ChainBase.torrent_files)["return"] == Optional[List[DownloaderFile]]
assert get_type_hints(is_webpush_subscription_gone)["error"] is WebPushError
assert loaded_provider_modules() == []
"""
    env = os.environ.copy()
    env["CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        f"轻量注解反射失败：\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )


def test_manifest_metadata_matches_legacy_module_class_contract(tmp_path: Path) -> None:
    """manifest 投影必须与插件仍可调用的模块类 metadata 完全一致。"""
    project_root = Path(__file__).parents[1]
    code = """
from app.testing.bootstrap import prepare_backend
prepare_backend()

from app.db.oper.systemconfig import SystemConfigOper

SystemConfigOper.get = lambda self, key=None: {} if key is None else []

from app.runtime.config import settings
settings.ACOUSTID_API_KEY = None
settings.FANART_API_KEY = None

from app.runtime.extensions.module.manager import ModuleManager

manager = ModuleManager()
modules = manager.get_modules()
assert len(modules) == len(manager.list_specs()) == 40
for spec in manager.list_specs():
    implementation = modules[spec.id]
    assert implementation.get_name() == spec.metadata["name"]
    assert implementation.get_type().value == spec.metadata["type"]
    assert implementation.get_subtype().name == spec.metadata["subtype"]
    assert implementation.get_priority() == spec.metadata["priority"]
manager.shutdown()
"""
    env = os.environ.copy()
    env["CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", "import sys\n" + code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        f"模块 metadata 兼容检查失败：\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )
