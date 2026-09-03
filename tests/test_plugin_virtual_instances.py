"""虚拟插件实例的持久化、加载和创建行为测试。"""

import sys
from types import ModuleType, SimpleNamespace

from app.runtime.extensions.plugin.clone import PluginCloneService
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


def _make_directory() -> PluginInstanceDirectory:
    """构造进程内插件实例描述符表，供分身持久化测试使用。"""
    records: dict[str, PluginInstance] = {}
    return PluginInstanceDirectory(
        get=records.get,
        list_all=lambda: list(records.values()),
        list_by_source=lambda source_plugin_id: [
            record
            for record in records.values()
            if record.source_plugin_id == source_plugin_id
        ],
        save=lambda instance: records.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: records.pop(instance_id, None) is not None,
    )


def _logger() -> SimpleNamespace:
    """提供加载器测试所需的最小日志对象。"""
    return SimpleNamespace(
        debug=lambda *_args: None,
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
    )


def test_instance_store_keeps_virtual_instances_out_of_installed_list():
    """虚拟实例使用独立配置键，不改写存量物理插件安装清单。"""
    values = {SystemConfigKey.UserInstalledPlugins: ["DemoPlugin"]}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory = _make_directory()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_name="工作实例",
    )
    store.save(instance)

    assert store.get("DemoPluginWork") == instance
    assert store.for_source("DemoPlugin") == [instance]
    assert values[SystemConfigKey.UserInstalledPlugins] == ["DemoPlugin"]
    assert store.delete("DemoPluginWork") is True
    assert store.all() == {}


def test_host_binding_does_not_leak_into_clone_list_or_installed_list():
    """本体的版本绑定与分身共用一张表，但不得出现在分身清单或已安装清单里。"""
    values = {SystemConfigKey.UserInstalledPlugins: ["DemoPlugin"]}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory = _make_directory()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    store.save(clone)

    store.save_host(
        PluginInstance(
            instance_id="DemoPlugin",
            source_plugin_id="DemoPlugin",
            follow_current_version=False,
            plugin_version="1.0.0",
        )
    )

    assert store.all() == {"DemoPluginWork": clone}
    assert store.for_source("DemoPlugin") == [clone]
    assert store.get("DemoPlugin") is None
    assert values[SystemConfigKey.UserInstalledPlugins] == ["DemoPlugin"]
    assert store.get_host("DemoPlugin") is not None


def test_all_hosts_batches_host_binding_records_without_leaking_clones():
    """``all_hosts`` 一次性返回全部本体绑定记录，且不得混入分身实例。"""
    values = {SystemConfigKey.UserInstalledPlugins: ["DemoPlugin", "OtherPlugin"]}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory = _make_directory()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    store.save(PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin"))
    demo_host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    other_host = PluginInstance(
        instance_id="OtherPlugin",
        source_plugin_id="OtherPlugin",
        is_default_target=True,
    )
    store.save_host(demo_host)
    store.save_host(other_host)

    hosts = store.all_hosts()

    assert hosts == {
        "DemoPlugin": demo_host.model_copy(update={"mode": "host"}),
        "OtherPlugin": other_host.model_copy(update={"mode": "host"}),
    }


def test_loader_executes_each_instance_in_an_isolated_module_namespace(
    tmp_path,
    monkeypatch,
):
    """两个实例共享磁盘源码，但模块全局状态、类身份和相对导入互相隔离。"""
    source_dir = tmp_path / "demoplugin"
    source_dir.mkdir()
    (source_dir / "state.py").write_text("state = []\n", encoding="utf-8")
    (source_dir / "__init__.py").write_text(
        "from app.plugins.demoplugin.state import state\n"
        "class DemoPlugin:\n"
        "    plugin_name = 'Demo'\n"
        "    plugin_desc = 'Source'\n"
        "    plugin_icon = 'source.svg'\n"
        "    plugin_config_prefix = 'demo_'\n"
        "    def init_plugin(self, _config):\n"
        "        state.append(self.__class__.__name__)\n",
        encoding="utf-8",
    )
    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
    )
    validator = lambda candidate: hasattr(candidate, "init_plugin")
    import app.plugins as plugin_package

    source_module = ModuleType("app.plugins.demoplugin")
    source_module.__path__ = [str(source_dir)]
    monkeypatch.setitem(sys.modules, "app.plugins.demoplugin", source_module)
    monkeypatch.setattr(plugin_package, "demoplugin", source_module, raising=False)

    work_class = loader.load_instance(
        PluginInstance(
            instance_id="DemoPluginWork",
            source_plugin_id="DemoPlugin",
            plugin_name="工作实例",
        ),
        validator,
    )[0]
    home_class = loader.load_instance(
        PluginInstance(
            instance_id="DemoPluginHome",
            source_plugin_id="DemoPlugin",
            plugin_desc="家庭实例",
        ),
        validator,
    )[0]
    work_class().init_plugin({})
    home_class().init_plugin({})

    assert work_class.__name__ == "DemoPluginWork"
    assert home_class.__name__ == "DemoPluginHome"
    assert work_class.__qualname__ == "DemoPlugin"
    assert work_class.__module__ == "app.plugins.demopluginwork"
    assert home_class.__module__ == "app.plugins.demopluginhome"
    assert work_class.plugin_source_id == "DemoPlugin"
    assert work_class.plugin_name == "工作实例"
    assert work_class.plugin_config_prefix == "demopluginwork_"
    assert work_class.__dict__["init_plugin"].__globals__["state"] == [
        "DemoPluginWork"
    ]
    assert home_class.__dict__["init_plugin"].__globals__["state"] == [
        "DemoPluginHome"
    ]
    assert sys.modules["app.plugins.demoplugin"] is source_module
    assert plugin_package.demoplugin is source_module


def test_loader_runtime_gate_only_rejects_explicit_incompatible_declarations(
    tmp_path,
    monkeypatch,
):
    """运行目录缺少声明或 runtime 为空时保持历史插件可加载。"""
    from app.runtime.extensions.plugin import loader as loader_module

    monkeypatch.setattr(
        loader_module,
        "get_runtime_setting",
        lambda key: "v3" if key == "VERSION_FLAG" else None,
    )
    monkeypatch.setattr(loader_module, "is_free_threaded_runtime", lambda: True)

    missing = tmp_path / "missing"
    missing.mkdir()
    assert PluginLoader._is_runtime_compatible(missing)

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "package.json").write_text('{"runtime": {}}', encoding="utf-8")
    assert PluginLoader._is_runtime_compatible(empty)

    rejected = tmp_path / "rejected"
    rejected.mkdir()
    (rejected / "package.json").write_text('{"v3t": false}', encoding="utf-8")
    assert not PluginLoader._is_runtime_compatible(rejected)


def test_clone_service_persists_descriptor_without_copying_source_package():
    """创建分身只写实例描述和隔离配置，并始终跟随源插件版本。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True, "token": "secret"}}
    reloaded: list[str] = []
    removed: list[str] = []

    class DemoPlugin:
        """提供创建用例需要的最小源插件类。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: plugin_id in instances,
        source_plugin_id=lambda plugin_id: plugin_id,
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        delete_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: configs.get(plugin_id, {}),
        save_config=lambda plugin_id, config: not configs.__setitem__(plugin_id, config),
        delete_config=lambda plugin_id: configs.pop(plugin_id, None) is not None,
        reload_plugin=lambda plugin_id: (
            reloaded.append(plugin_id) or PluginRuntimeStatus.ACTIVE
        ),
        remove_plugin=removed.append,
        log=_logger(),
    )

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Work",
        name="工作实例",
        description="独立配置",
        version="9.9.9",
    )

    assert success is True
    assert clone_id == "DemoPluginwork"
    assert instances[clone_id].source_plugin_id == "DemoPlugin"
    assert configs[clone_id] == {
        "enable": False,
        "enabled": False,
        "token": "secret",
    }
    assert reloaded == [clone_id]
    assert removed == []


def test_clone_service_rolls_back_descriptor_and_config_after_load_failure():
    """实例首次加载失败时不留下不可见描述或孤立配置。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enabled": True}}
    removed: list[str] = []

    class DemoPlugin:
        """提供失败回滚用例需要的最小源插件类。"""

    service = PluginCloneService(
        plugin_class=lambda _plugin_id: DemoPlugin,
        plugin_exists=lambda plugin_id: plugin_id in instances,
        source_plugin_id=lambda plugin_id: plugin_id,
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        delete_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: configs.get(plugin_id, {}),
        save_config=lambda plugin_id, config: not configs.__setitem__(plugin_id, config),
        delete_config=lambda plugin_id: configs.pop(plugin_id, None) is not None,
        reload_plugin=lambda _plugin_id: PluginRuntimeStatus.LOAD_FAILED,
        remove_plugin=removed.append,
        log=_logger(),
    )

    success, message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Broken",
        name="失败实例",
        description="",
    )

    assert success is False
    assert "加载失败" in message
    assert instances == {}
    assert configs == {"DemoPlugin": {"enabled": True}}
    assert removed == ["DemoPluginbroken"]
