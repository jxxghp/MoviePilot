"""插件实例版本绑定字段、加载期版本解析与已生效版本登记测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)
from app.runtime.extensions.plugin.version import (
    plugin_version_dir_name,
    write_plugin_versions_manifest,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


def _logger() -> SimpleNamespace:
    """提供加载器与生命周期测试所需的最小日志端口。"""
    return SimpleNamespace(
        debug=lambda *_args: None,
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
    )


def _make_loader(plugins_root: Path, **overrides) -> PluginLoader:
    """构造只需最小日志端口的加载器实例，可覆盖本体版本绑定查询端口。"""
    return PluginLoader(
        plugins_root=plugins_root,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
        **overrides,
    )


class _FakeInstanceDirectory:
    """进程内插件实例描述符表，记录写入调用轨迹供断言使用。"""

    def __init__(self) -> None:
        self.records: dict[str, PluginInstance] = {}
        self.saved: list[PluginInstance] = []
        self.deleted: list[str] = []

    def get(self, instance_id: str) -> PluginInstance | None:
        """按实例 ID 读取单条描述。"""
        return self.records.get(instance_id)

    def list_all(self) -> list[PluginInstance]:
        """列出全部描述。"""
        return list(self.records.values())

    def list_by_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按源插件 ID 列出其全部描述。"""
        return [
            record
            for record in self.records.values()
            if record.source_plugin_id == source_plugin_id
        ]

    def save(self, instance: PluginInstance) -> None:
        """新增或更新一条描述并记录调用轨迹。"""
        self.records[instance.instance_id] = instance
        self.saved.append(instance)

    def delete(self, instance_id: str) -> bool:
        """删除一条描述并记录调用轨迹。"""
        removed = self.records.pop(instance_id, None)
        if removed is not None:
            self.deleted.append(instance_id)
        return removed is not None

    def port(self) -> PluginInstanceDirectory:
        """构造绑定到本实例状态的持久化端口。"""
        return PluginInstanceDirectory(
            get=self.get,
            list_all=self.list_all,
            list_by_source=self.list_by_source,
            save=self.save,
            delete=self.delete,
        )


def _write_version(
    plugins_root: Path,
    plugin_id: str,
    version: str,
    *,
    class_name: str = "Versioned",
    marker: str,
) -> Path:
    """写入一个版本目录的最小可加载源码，marker 用于区分不同版本被加载到。"""
    plugin_root = plugins_root / plugin_id
    version_dir = plugin_root / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(
        f"class {class_name}:\n"
        f"    plugin_version = {version!r}\n"
        f"    marker = {marker!r}\n"
        "    def init_plugin(self, config=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    return version_dir


def _write_manifest(plugin_root: Path, entries: list[tuple[str, str]], current: str | None) -> None:
    """写入版本元信息文件。"""
    versions = [
        {
            "version": version,
            "directory": directory,
            "installed_at": "2026-01-01T00:00:00+00:00",
            "source": "test",
        }
        for version, directory in entries
    ]
    write_plugin_versions_manifest(plugin_root, versions, current)


@pytest.fixture(autouse=True)
def _isolate_plugin_modules():
    """回收测试期间手动导入的临时插件模块，避免污染其它用例的模块缓存。"""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("app.plugins."):
            sys.modules.pop(name, None)


# 一、PluginInstance 版本绑定字段


def test_plugin_instance_defaults_to_no_effective_version_and_follows_current():
    """新建实例默认未生效任何版本且跟随插件当前版本。"""
    instance = PluginInstance(instance_id="DemoWork", source_plugin_id="Demo")

    assert instance.plugin_version is None
    assert instance.follow_current_version is True


def test_instance_store_tolerates_legacy_payload_missing_version_fields():
    """兜底导入时，存量数据缺少版本绑定字段仍按默认值容错反序列化，不丢弃该实例。"""
    values = {
        SystemConfigKey.PluginInstances: {
            "DemoWork": {
                "instance_id": "DemoWork",
                "source_plugin_id": "Demo",
                "plugin_name": "工作实例",
            }
        }
    }
    storage = PluginStorage(read=values.get, write=lambda key, value: values.__setitem__(key, value))
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)

    instance = store.get("DemoWork")

    assert instance is not None
    assert instance.plugin_version is None
    assert instance.follow_current_version is True


# 二、PluginInstanceStore 已生效版本登记


def test_record_effective_version_writes_when_changed():
    """成功启动的版本与已登记值不同时才写入持久化。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)
    store.save(PluginInstance(instance_id="DemoWork", source_plugin_id="Demo"))

    store.record_effective_version("DemoWork", "1.2.0")

    assert store.get("DemoWork").plugin_version == "1.2.0"


def test_record_effective_version_skips_write_when_unchanged():
    """已生效版本与本次值相同时不产生新的持久化写入。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    directory = _FakeInstanceDirectory()
    store = PluginInstanceStore(storage=lambda: storage, directory=directory.port)
    store.save(PluginInstance(instance_id="DemoWork", source_plugin_id="Demo", plugin_version="1.2.0"))
    directory.saved.clear()

    store.record_effective_version("DemoWork", "1.2.0")

    assert directory.saved == []


def test_record_effective_version_ignores_ids_without_instance_descriptor():
    """物理插件的分身与本体都没有实例描述时静默跳过，不因找不到实例而报错。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)

    store.record_effective_version("PhysicalPlugin", "1.0.0")

    assert store.all() == {}


# 五、PluginInstanceStore 本体版本绑定


def test_host_binding_is_isolated_from_clone_views():
    """本体的版本绑定记录不出现在 all()/get()/for_source() 这些分身专用视图里。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)

    store.save_host(
        PluginInstance(
            instance_id="DemoPlugin",
            source_plugin_id="DemoPlugin",
            follow_current_version=False,
            plugin_version="1.0.0",
        )
    )

    assert store.get("DemoPlugin") is None
    assert store.all() == {}
    assert store.for_source("DemoPlugin") == []
    host = store.get_host("DemoPlugin")
    assert host is not None
    assert host.mode == "host"
    assert host.follow_current_version is False
    assert host.plugin_version == "1.0.0"


def test_host_binding_defaults_to_none_when_never_bound():
    """从未显式绑定过版本的本体读取为 None，而不是一条隐式默认记录。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)

    assert store.get_host("DemoPlugin") is None


def test_record_effective_version_updates_existing_host_binding():
    """本体已被绑定过版本时，成功启动会更新其已生效版本。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)
    store.save_host(PluginInstance(instance_id="DemoPlugin", source_plugin_id="DemoPlugin"))

    store.record_effective_version("DemoPlugin", "1.2.0")

    assert store.get_host("DemoPlugin").plugin_version == "1.2.0"


def test_record_effective_version_does_not_create_host_binding_on_first_start():
    """本体从未被显式绑定过版本时，成功启动不会隐式创建一条绑定记录。"""
    storage = PluginStorage(read=lambda _key: None, write=lambda _key, _value: None)
    store = PluginInstanceStore(storage=lambda: storage, directory=_FakeInstanceDirectory().port)

    store.record_effective_version("DemoPlugin", "1.2.0")

    assert store.get_host("DemoPlugin") is None


def test_store_bootstraps_legacy_instances_once_when_table_is_empty():
    """新表为空而旧 systemconfig 单键非空时，首次访问按旧内容导入一次，且不重复导入。"""
    values = {
        SystemConfigKey.PluginInstances: {
            "DemoWork": {"instance_id": "DemoWork", "source_plugin_id": "Demo"},
        }
    }
    storage = PluginStorage(read=values.get, write=lambda key, value: values.__setitem__(key, value))
    directory = _FakeInstanceDirectory()
    store = PluginInstanceStore(storage=lambda: storage, directory=directory.port)

    first = store.all()
    values[SystemConfigKey.PluginInstances] = {
        "DemoWork": {"instance_id": "DemoWork", "source_plugin_id": "Demo"},
        "DemoHome": {"instance_id": "DemoHome", "source_plugin_id": "Demo"},
    }
    second = store.all()

    assert set(first) == {"DemoWork"}
    assert set(second) == {"DemoWork"}
    assert len(directory.saved) == 1


def test_store_skips_bootstrap_import_when_table_already_has_rows():
    """新表已有内容时不再导入旧 systemconfig 单键，避免覆盖已迁移或已改动的数据。"""
    values = {
        SystemConfigKey.PluginInstances: {
            "DemoWork": {"instance_id": "DemoWork", "source_plugin_id": "Demo"},
        }
    }
    storage = PluginStorage(read=values.get, write=lambda key, value: values.__setitem__(key, value))
    directory = _FakeInstanceDirectory()
    directory.records["DemoHome"] = PluginInstance(
        instance_id="DemoHome", source_plugin_id="Demo"
    )
    store = PluginInstanceStore(storage=lambda: storage, directory=directory.port)

    instances = store.all()

    assert set(instances) == {"DemoHome"}
    assert directory.saved == []


# 三、加载期版本解析与失败回退


def test_load_instance_follows_manifest_current_version_by_default(tmp_path: Path):
    """跟随当前版本时，加载器按版本元信息登记的当前版本取源码。"""
    _write_version(tmp_path, "versioned", "1.0.0", marker="old")
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(
        tmp_path / "versioned",
        [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")],
        current="2.0.0",
    )

    instance = PluginInstance(instance_id="VersionedWork", source_plugin_id="versioned")
    plugins = _make_loader(tmp_path).load_instance(
        instance, lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert plugins[0].plugin_version == "2.0.0"
    assert plugins[0].marker == "new"


def test_load_instance_uses_bound_version_when_not_following_current(tmp_path: Path):
    """不跟随当前版本时，加载器固定使用绑定版本的源码，而不是清单当前版本。"""
    _write_version(tmp_path, "versioned", "1.0.0", marker="old")
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(
        tmp_path / "versioned",
        [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")],
        current="2.0.0",
    )

    instance = PluginInstance(
        instance_id="VersionedWork",
        source_plugin_id="versioned",
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    plugins = _make_loader(tmp_path).load_instance(
        instance, lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert plugins[0].plugin_version == "1.0.0"
    assert plugins[0].marker == "old"


def test_load_instance_falls_back_to_current_when_bound_directory_missing(tmp_path: Path):
    """绑定版本目录已不存在时回落当前版本，而不是直接判定加载失败。"""
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(tmp_path / "versioned", [("2.0.0", "v2_0_0")], current="2.0.0")

    instance = PluginInstance(
        instance_id="VersionedWork",
        source_plugin_id="versioned",
        follow_current_version=False,
        plugin_version="9.9.9",
    )
    warnings: list[str] = []
    loader = _make_loader(tmp_path)
    loader._logger.warning = warnings.append

    plugins = loader.load_instance(instance, lambda candidate: hasattr(candidate, "init_plugin"))

    assert plugins[0].plugin_version == "2.0.0"
    assert warnings and "9.9.9" in warnings[0]


def test_load_instance_explicit_version_overrides_binding(tmp_path: Path):
    """显式指定版本时优先于实例自身绑定，供失败回退重试指定一个具体版本。"""
    _write_version(tmp_path, "versioned", "1.0.0", marker="old")
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(
        tmp_path / "versioned",
        [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")],
        current="2.0.0",
    )

    instance = PluginInstance(instance_id="VersionedWork", source_plugin_id="versioned")
    plugins = _make_loader(tmp_path).load_instance(
        instance,
        lambda candidate: hasattr(candidate, "init_plugin"),
        version="1.0.0",
    )

    assert plugins[0].plugin_version == "1.0.0"
    assert plugins[0].marker == "old"


def test_load_host_follows_manifest_current_version_without_binding(tmp_path: Path):
    """源插件本体从未被绑定过版本时，加载器按版本元信息登记的当前版本取源码。"""
    _write_version(tmp_path, "versioned", "1.0.0", marker="old")
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(
        tmp_path / "versioned",
        [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")],
        current="2.0.0",
    )

    plugins = _make_loader(tmp_path).load(
        "versioned", ["versioned"], lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert plugins[0].plugin_version == "2.0.0"
    assert plugins[0].marker == "new"


def test_load_host_uses_bound_version_when_not_following_current(tmp_path: Path):
    """本体绑定为不跟随当前版本时，加载器固定使用绑定版本的源码。"""
    _write_version(tmp_path, "versioned", "1.0.0", marker="old")
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(
        tmp_path / "versioned",
        [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")],
        current="2.0.0",
    )
    binding = PluginInstance(
        instance_id="versioned",
        source_plugin_id="versioned",
        mode="host",
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    loader = _make_loader(tmp_path, host_binding=lambda plugin_id: binding if plugin_id == "versioned" else None)

    plugins = loader.load(
        "versioned", ["versioned"], lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert plugins[0].plugin_version == "1.0.0"
    assert plugins[0].marker == "old"


def test_load_host_falls_back_to_current_when_bound_directory_missing(tmp_path: Path):
    """本体绑定的版本目录已不存在时回落当前版本，而不是让本体加载失败。"""
    _write_version(tmp_path, "versioned", "2.0.0", marker="new")
    _write_manifest(tmp_path / "versioned", [("2.0.0", "v2_0_0")], current="2.0.0")
    binding = PluginInstance(
        instance_id="versioned",
        source_plugin_id="versioned",
        mode="host",
        follow_current_version=False,
        plugin_version="9.9.9",
    )
    warnings: list[str] = []
    loader = _make_loader(tmp_path, host_binding=lambda plugin_id: binding if plugin_id == "versioned" else None)
    loader._logger.warning = warnings.append

    plugins = loader.load(
        "versioned", ["versioned"], lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert plugins[0].plugin_version == "2.0.0"
    assert warnings and "9.9.9" in warnings[0]


# 四、PluginLifecycle 启动成功后登记已生效版本


def _build_lifecycle(*, load_plugins, record_instance_version) -> PluginLifecycle:
    """构造只暴露版本登记端口的最小生命周期实例。"""
    from app.runtime.extensions.plugin.database import PluginDatabase

    return PluginLifecycle(
        classes={},
        running={},
        load_plugins=load_plugins,
        installed_plugins=lambda: ["VersionedWork"],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: True,
        clear_modules=lambda _plugin_id: None,
        clear_tools=lambda: None,
        enable_events=lambda _plugin: None,
        disable_events=lambda _plugin: None,
        runtime_status_writer=lambda _plugin_id, _status: None,
        database=lambda: PluginDatabase(),
        log=_logger(),
        event_sender=lambda *_args, **_kwargs: None,
        record_instance_version=record_instance_version,
    )


def _versioned_plugin_class(version: str):
    """构造声明指定版本号的最小插件类。"""
    return type(
        "VersionedWork",
        (),
        {
            "plugin_name": "版本化实例",
            "plugin_version": version,
            "init_plugin": lambda self, _config=None: None,
            "get_state": staticmethod(lambda: True),
        },
    )


def test_lifecycle_records_loaded_class_version_on_successful_start():
    """启动成功后把已加载类声明的版本登记为该实例的已生效版本。"""
    recorded: list[tuple[str, str]] = []
    lifecycle = _build_lifecycle(
        load_plugins=lambda _pid, _installed, _check, _version=None: [
            _versioned_plugin_class("2.0.0")
        ],
        record_instance_version=lambda instance_id, version: recorded.append(
            (instance_id, version)
        ),
    )

    results = lifecycle.start("VersionedWork")

    assert results["VersionedWork"] == PluginRuntimeStatus.ACTIVE
    assert recorded == [("VersionedWork", "2.0.0")]


def test_lifecycle_does_not_record_version_when_start_fails():
    """启动失败时不登记任何版本，保持已生效版本原值不变。"""
    recorded: list[tuple[str, str]] = []

    def _raising_init(_self, _config=None):
        raise RuntimeError("boom")

    broken_class = type(
        "VersionedWork",
        (),
        {
            "plugin_name": "版本化实例",
            "plugin_version": "2.0.0",
            "init_plugin": _raising_init,
            "get_state": staticmethod(lambda: True),
        },
    )
    lifecycle = _build_lifecycle(
        load_plugins=lambda _pid, _installed, _check, _version=None: [broken_class],
        record_instance_version=lambda instance_id, version: recorded.append(
            (instance_id, version)
        ),
    )

    results = lifecycle.start("VersionedWork")

    assert results["VersionedWork"] == PluginRuntimeStatus.LOAD_FAILED
    assert recorded == []


def test_lifecycle_start_threads_explicit_version_to_load_plugins():
    """显式 version 参数会原样传给 load_plugins，供版本切换重试使用。"""
    seen_versions: list = []

    def _load_plugins(_pid, _installed, _check, version=None):
        seen_versions.append(version)
        return [_versioned_plugin_class(version or "2.0.0")]

    lifecycle = _build_lifecycle(
        load_plugins=_load_plugins,
        record_instance_version=lambda *_args: None,
    )

    lifecycle.start("VersionedWork", version="1.0.0")

    assert seen_versions == ["1.0.0"]
