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
    """构造进程内插件实例表，供分身持久化测试使用。

    停用的行照常留在各个读取口里：是否应当被实例化由 is_enabled 单独表达，读取口
    不替调用方把停用的行藏起来，运行期取数走 list_enabled。
    """
    records: dict[str, PluginInstance] = {}

    def _set_enabled(instance_id: str, is_enabled: bool) -> bool:
        """写入启用位。"""
        record = records.get(instance_id)
        if record is None:
            return False
        records[instance_id] = record.model_copy(update={"is_enabled": is_enabled})
        return True

    return PluginInstanceDirectory(
        get=records.get,
        list_all=lambda: list(records.values()),
        list_enabled=lambda: [
            record for record in records.values() if record.is_enabled
        ],
        list_by_source=lambda source_plugin_id: [
            record
            for record in records.values()
            if record.source_plugin_id == source_plugin_id
        ],
        save=lambda instance: records.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: records.pop(instance_id, None) is not None,
        set_enabled=_set_enabled,
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
    assert store.disable("DemoPluginWork") is True
    assert store.enabled() == {}
    # 停用不删行：配置与展示信息留在原处，卡片仍可见
    assert set(store.all()) == {"DemoPluginWork"}


def test_legacy_instances_are_not_reimported_after_being_deleted():
    """删光分身后重启不得从旧 systemconfig 键把它们导回来。

    旧键刻意保留作回滚依据、从不清理，若以「表为空」作为兜底导入的判据，
    用户每删光一次分身、下次启动就会复活一次。
    """
    values = {
        SystemConfigKey.UserInstalledPlugins: ["DemoPlugin"],
        SystemConfigKey.PluginInstances: {
            "DemoPluginWork": {
                "instance_id": "DemoPluginWork",
                "source_plugin_id": "DemoPlugin",
            }
        },
    }
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory = _make_directory()

    # 首次访问：旧键内容被导入独立表
    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork"}

    # 用户彻底清理掉全部分身，表重新变空
    assert first.purge("DemoPluginWork") is True
    assert first.all() == {}

    # 进程重启：新建 store 重新走一次兜底导入判定
    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert second.all() == {}
    assert values[SystemConfigKey.PluginInstances] is not None


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
            pinned_version="1.0.0",
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
        pinned_version="1.0.0",
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
        "DemoPlugin": demo_host,
        "OtherPlugin": other_host,
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
    deleted_data: list[str] = []

    class DemoPlugin:
        """提供创建用例需要的最小源插件类。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: plugin_id in instances,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda _plugin_id: (),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        purge_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: configs.get(plugin_id, {}),
        save_config=lambda plugin_id, config: not configs.__setitem__(plugin_id, config),
        delete_data=lambda plugin_id: deleted_data.append(plugin_id),
        has_data=lambda _plugin_id: False,
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


def test_retarget_module_identity_also_retargets_methods_defined_in_class_bodies():
    """类体内定义的方法也要改到分身模块名下。

    事件处理器的注册键取自函数所在模块名（``inspect.getmodule(target).__name__``）。
    类体内的函数是类的属性而非模块属性，只遍历模块顶层改不到它们，漏改会让分身
    与本体注册到同一个 handler 键上：分身收不到事件、停本体会把分身一起停掉。
    """
    source_name = "app.plugins.demoplugin.core"
    instance_name = "app.plugins.demopluginwork.core"
    module = ModuleType(source_name)

    class DemoPlugin:
        """提供一个类体内定义的处理器方法。"""

        def on_event(self):
            """占位处理器。"""

        @staticmethod
        def on_static_event():
            """静态处理器同样要被改写。"""

    DemoPlugin.__module__ = source_name
    DemoPlugin.on_event.__module__ = source_name
    DemoPlugin.on_static_event.__module__ = source_name
    module.DemoPlugin = DemoPlugin

    PluginLoader._retarget_module_identity(module, source_name, instance_name)

    assert DemoPlugin.__module__ == instance_name
    assert DemoPlugin.on_event.__module__ == instance_name
    assert DemoPlugin.on_static_event.__module__ == instance_name


def test_clone_service_rejects_cloning_from_a_clone_own_id():
    """禁止以分身自身的实例 ID 作为来源再创建分身，只能对源插件本体克隆。"""
    instances: dict[str, PluginInstance] = {}

    class DemoPluginWork:
        """分身在 registry 里同样是已登记的类，模拟这一事实。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPluginWork,
        plugin_exists=lambda plugin_id: plugin_id in instances,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: (
            "DemoPlugin" if plugin_id == "DemoPluginWork" else plugin_id
        ),
        installed_versions=lambda _plugin_id: (),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        purge_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: {},
        save_config=lambda plugin_id, config: True,
        delete_data=lambda plugin_id: None,
        has_data=lambda _plugin_id: False,
        reload_plugin=lambda plugin_id: PluginRuntimeStatus.ACTIVE,
        remove_plugin=lambda plugin_id: None,
        log=_logger(),
    )

    success, message = service.clone(
        plugin_id="DemoPluginWork",
        suffix="Nested",
        name="嵌套分身",
        description="",
    )

    assert success is False
    assert "分身" in message
    assert instances == {}


def test_clone_service_rejects_duplicate_even_when_nothing_is_running():
    """已有同名分身时必须拒绝，哪怕它当前没在运行。

    判存若依赖运行态，源插件本次加载失败时已有分身会被判成「不存在」而放行，
    随后覆盖它的描述符，再由回滚把它连同配置一起删掉。
    """
    existing = PluginInstance(
        instance_id="DemoPluginwork",
        source_plugin_id="DemoPlugin",
        plugin_name="既有分身",
    )
    instances: dict[str, PluginInstance] = {"DemoPluginwork": existing}
    configs = {"DemoPlugin": {"enable": True}}
    deleted_instances: list[str] = []

    class DemoPlugin:
        """源插件类仍在注册表里，但没有任何实例在运行。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        # 运行态判存：没有实例在跑，一律回报「不存在」
        plugin_exists=lambda plugin_id: False,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda _plugin_id: (),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        purge_instance=lambda plugin_id: deleted_instances.append(plugin_id) or True,
        read_config=lambda plugin_id: configs.get(plugin_id, {}),
        save_config=lambda plugin_id, config: not configs.__setitem__(plugin_id, config),
        delete_data=lambda plugin_id: None,
        has_data=lambda _plugin_id: False,
        reload_plugin=lambda plugin_id: PluginRuntimeStatus.ACTIVE,
        remove_plugin=lambda plugin_id: None,
        log=_logger(),
    )

    success, message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Work",
        name="重复创建",
        description="",
    )

    assert success is False
    assert "已存在" in message
    assert instances["DemoPluginwork"] is existing
    assert deleted_instances == []


def test_clone_service_rolls_back_descriptor_and_config_after_load_failure():
    """实例首次加载失败时不留下不可见描述或孤立配置。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enabled": True}}
    removed: list[str] = []
    deleted_data: list[str] = []

    class DemoPlugin:
        """提供失败回滚用例需要的最小源插件类。"""

    service = PluginCloneService(
        # 注册表只认源插件 ID，分身 ID 在创建成功前不会有已登记的类
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: plugin_id in instances,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda _plugin_id: (),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        # 配置长在实例行上，彻底删行就把配置一并带走
        purge_instance=lambda plugin_id: (
            instances.pop(plugin_id, None) is not None,
            configs.pop(plugin_id, None),
        )[0],
        read_config=lambda plugin_id: configs.get(plugin_id, {}),
        save_config=lambda plugin_id, config: not configs.__setitem__(plugin_id, config),
        delete_data=lambda plugin_id: deleted_data.append(plugin_id),
        has_data=lambda _plugin_id: False,
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


def test_clone_service_pins_the_requested_version():
    """锚定版本创建分身时，版本绑定要落进实例描述。

    新建分身此前恒为「跟随当前版本」——建实例时根本不写版本绑定字段，锚定版本
    因此无从生效，分身会随本体更新一起漂走。
    """
    instances: dict[str, PluginInstance] = {}

    class DemoPlugin:
        """提供创建用例需要的最小源插件类。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: False,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda plugin_id: ("1.0.0", "2.0.0"),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        purge_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: {},
        save_config=lambda plugin_id, config: True,
        delete_data=lambda plugin_id: None,
        has_data=lambda _plugin_id: False,
        reload_plugin=lambda plugin_id: PluginRuntimeStatus.ACTIVE,
        remove_plugin=lambda plugin_id: None,
        log=_logger(),
    )

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Pinned",
        name="锚定分身",
        description="",
        pinned_version="1.0.0",
    )

    assert success is True
    assert instances[clone_id].pinned_version is not None
    assert instances[clone_id].pinned_version == "1.0.0"


def test_clone_service_rejects_pinning_a_version_that_is_not_installed():
    """锚定一个未安装的版本必须拒绝，不能建出指向空目录的分身。"""
    instances: dict[str, PluginInstance] = {}

    class DemoPlugin:
        """提供创建用例需要的最小源插件类。"""

    service = PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: False,
        get_instance=instances.get,
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda _plugin_id: False,
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda plugin_id: ("1.0.0",),
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        purge_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        read_config=lambda plugin_id: {},
        save_config=lambda plugin_id, config: True,
        delete_data=lambda plugin_id: None,
        has_data=lambda _plugin_id: False,
        reload_plugin=lambda plugin_id: PluginRuntimeStatus.ACTIVE,
        remove_plugin=lambda plugin_id: None,
        log=_logger(),
    )

    success, message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Missing",
        name="",
        description="",
        pinned_version="9.9.9",
    )

    assert success is False
    assert "9.9.9" in message
    assert instances == {}


def test_deleting_a_clone_leaves_no_config_business_data_or_own_database():
    """分身删除后配置、业务数据与自有库都不得残留。

    残留会让「用同一后缀重建分身」静默继承上一次的配置和数据，用户看到的是
    一个刚建好却带着旧状态的实例。
    """
    from app.runtime.extensions.plugin.database import PluginDatabase
    from app.runtime.extensions.plugin.storage import PluginConfigStore

    config_keys: dict[str, dict] = {"plugin.DemoPluginWork": {"enable": True, "token": "x"}}
    business_data: dict[str, str] = {"DemoPluginWork": "业务数据"}
    destroyed: list[str] = []

    storage = PluginStorage(
        read=config_keys.get,
        write=lambda key, value: config_keys.__setitem__(key, value),
        delete=lambda key: config_keys.pop(key, None) is not None,
        delete_data=lambda plugin_id: business_data.pop(plugin_id, None) is not None,
    )
    store = PluginConfigStore(
        storage=lambda: storage,
        database=lambda: PluginDatabase(destroy=destroyed.append),
        # 卸载序列先停止插件，此时运行态已注销，故一律按 force 删除
        plugin_exists=lambda _plugin_id: False,
    )

    assert store.delete("DemoPluginWork", force=True) is True
    assert store.delete_data("DemoPluginWork", force=True) is True

    assert config_keys == {}
    assert business_data == {}
    assert destroyed == ["DemoPluginWork"]


def _restore_service(instances, configs, data_ids, deleted_data):
    """构造一个带残留状态探测端口的分身创建服务。"""

    class DemoPlugin:
        """提供创建用例需要的最小源插件类。"""

    return PluginCloneService(
        plugin_class=lambda plugin_id: DemoPlugin if plugin_id == "DemoPlugin" else None,
        plugin_exists=lambda plugin_id: False,
        get_instance=lambda instance_id: (
            record
            if (record := instances.get(instance_id)) and record.is_enabled
            else None
        ),
        get_any_instance=instances.get,
        all_instances_for_source=lambda _source: list(instances.values()),
        disable_instance=lambda instance_id: bool(
            instances.__setitem__(
                instance_id,
                instances[instance_id].model_copy(update={"is_enabled": False}),
            )
            or True
        ),
        source_plugin_id=lambda plugin_id: plugin_id,
        installed_versions=lambda _plugin_id: ("1.0.0",),
        save_instance=lambda instance: instances.__setitem__(instance.instance_id, instance),
        purge_instance=lambda plugin_id: (
            instances.pop(plugin_id, None) is not None,
            configs.pop(plugin_id, None),
        )[0],
        read_config=configs.get,
        save_config=lambda plugin_id, config: configs.__setitem__(plugin_id, config) or True,
        delete_data=lambda plugin_id: deleted_data.append(plugin_id),
        has_data=lambda plugin_id: plugin_id in data_ids,
        reload_plugin=lambda plugin_id: PluginRuntimeStatus.ACTIVE,
        remove_plugin=lambda plugin_id: None,
        log=_logger(),
    )


def test_recreating_a_clone_restores_the_uninstalled_one_holding_that_id():
    """同后缀重建就是把已卸载的分身连同配置一起拿回来，而不是撞车被挡下。

    卸载只把启用位置假、那一行原样留着；判存若照旧把它当占用，用户就只剩「换个
    后缀」这一条路，留存的配置永远拿不回来。
    """
    instances: dict[str, PluginInstance] = {
        "DemoPlugintest": PluginInstance(
            instance_id="DemoPlugintest",
            source_plugin_id="DemoPlugin",
            plugin_name="旧名字",
            pinned_version="1.0.0",
            is_enabled=False,
        )
    }
    configs = {
        "DemoPlugin": {"enable": True, "token": "源插件"},
        "DemoPlugintest": {"enable": False, "token": "留存的"},
    }
    service = _restore_service(instances, configs, set(), [])

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
    )

    assert success is True
    assert clone_id == "DemoPlugintest"
    assert instances["DemoPlugintest"].is_enabled is True
    # 没填名称与版本时沿用卸载前登记的那一份
    assert instances["DemoPlugintest"].plugin_name == "旧名字"
    assert instances["DemoPlugintest"].pinned_version == "1.0.0"
    # 留存的配置不得被源插件模板盖掉
    assert configs["DemoPlugintest"] == {"enable": False, "token": "留存的"}


def test_recreating_a_clone_can_start_over_from_the_source_template():
    """显式要求全新时，留存的配置让位给源插件模板。"""
    instances: dict[str, PluginInstance] = {
        "DemoPlugintest": PluginInstance(
            instance_id="DemoPlugintest",
            source_plugin_id="DemoPlugin",
            plugin_name="旧名字",
            is_enabled=False,
        )
    }
    configs = {
        "DemoPlugin": {"enable": True, "token": "源插件"},
        "DemoPlugintest": {"enable": False, "token": "留存的"},
    }
    service = _restore_service(instances, configs, set(), [])

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="新名字",
        description="",
        restore_previous=False,
    )

    assert success is True
    assert instances[clone_id].plugin_name == "新名字"
    assert configs[clone_id] == {"enable": False, "enabled": False, "token": "源插件"}


def test_a_failed_restore_leaves_the_uninstalled_clone_untouched():
    """恢复途中加载失败，那一行要退回已卸载状态，而不是被整行抹掉。

    它是用户特意留着的配置，一次加载失败不该把它毁掉。
    """
    instances: dict[str, PluginInstance] = {
        "DemoPlugintest": PluginInstance(
            instance_id="DemoPlugintest",
            source_plugin_id="DemoPlugin",
            is_enabled=False,
        )
    }
    configs = {"DemoPlugin": {"enable": True}, "DemoPlugintest": {"token": "留存的"}}
    service = _restore_service(instances, configs, set(), [])
    service._reload_plugin = lambda _plugin_id: PluginRuntimeStatus.LOAD_FAILED

    success, _message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
    )

    assert success is False
    assert "DemoPlugintest" in instances
    assert configs["DemoPlugintest"] == {"token": "留存的"}


def test_a_new_clone_is_created_enabled_so_it_actually_loads():
    """建出的分身必须是启用状态，否则装载口按 is_enabled 取数时它永远不会被实例化。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True, "token": "源插件"}}
    service = _restore_service(instances, configs, set(), [])

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
    )

    assert success is True
    assert instances[clone_id].is_enabled is True
    # 模板来自源插件，插件自身的业务开关一律先关掉
    assert configs[clone_id] == {"enable": False, "enabled": False, "token": "源插件"}


def test_recreating_a_clone_can_discard_business_data_left_on_disk():
    """实例被彻底清理后磁盘上仍可能留着业务数据，显式选择清空时要先清掉它。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True, "token": "源插件"}}
    deleted_data: list[str] = []
    service = _restore_service(instances, configs, {"DemoPlugintest"}, deleted_data)

    success, clone_id = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
        restore_previous=False,
    )

    assert success is True
    assert deleted_data == ["DemoPlugintest"]
    assert configs[clone_id] == {"enable": False, "enabled": False, "token": "源插件"}


def test_a_failed_creation_keeps_inherited_business_data_for_another_attempt():
    """沿用残留数据时创建失败，回滚不得清掉那份数据——它不是本次的产物。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True}}
    deleted_data: list[str] = []
    service = _restore_service(instances, configs, {"DemoPlugintest"}, deleted_data)
    service._reload_plugin = lambda _plugin_id: PluginRuntimeStatus.LOAD_FAILED

    success, message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
    )

    assert success is False
    assert "失败" in message
    # 本次建出的实例行与配置要回滚，继承来的业务数据必须原地不动
    assert instances == {}
    assert deleted_data == []


def test_a_failed_creation_purges_business_data_it_created_itself():
    """没有继承任何残留时创建失败，本次可能建出的自有库与数据要一并销毁。"""
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True}}
    deleted_data: list[str] = []
    service = _restore_service(instances, configs, set(), deleted_data)
    service._reload_plugin = lambda _plugin_id: PluginRuntimeStatus.LOAD_FAILED

    success, _message = service.clone(
        plugin_id="DemoPlugin",
        suffix="Test",
        name="",
        description="",
    )

    assert success is False
    assert deleted_data == ["DemoPlugintest"]


def test_runtime_load_driver_only_instantiates_enabled_instances(tmp_path, monkeypatch):
    """运行期批量装载只取启用的配置，停用的分身登记在册但不得被实例化。

    这条不变量此前无人守：装载驱动一度按「行是否存在」取数，停用位形同虚设，
    卸载掉的分身照样会被加载起来。这里直接调用 build_plugin_runtime 装出来的
    那条驱动，而不是在测试里复刻一遍它的逻辑——复刻只能证明取数口本身对，
    证明不了驱动确实走的是那个取数口。
    """
    from app.runtime.extensions.plugin.runtime import (
        PluginRuntimeEnvironment,
        build_plugin_runtime,
    )

    source_dir = tmp_path / "demoplugin"
    source_dir.mkdir()
    (source_dir / "__init__.py").write_text(
        "class DemoPlugin:\n"
        "    plugin_name = 'Demo'\n"
        "    plugin_config_prefix = 'demo_'\n"
        "    def init_plugin(self, _config):\n"
        "        pass\n",
        encoding="utf-8",
    )
    import app.plugins as plugin_package

    source_module = ModuleType("app.plugins.demoplugin")
    source_module.__path__ = [str(source_dir)]
    monkeypatch.setitem(sys.modules, "app.plugins.demoplugin", source_module)
    monkeypatch.setattr(plugin_package, "demoplugin", source_module, raising=False)

    directory = _make_directory()
    for instance_id in ("DemoPluginOn", "DemoPluginOff"):
        directory.save(
            PluginInstance(
                instance_id=instance_id,
                source_plugin_id="DemoPlugin",
                is_enabled=True,
            )
        )
    assert directory.set_enabled("DemoPluginOff", False) is True

    values = {
        SystemConfigKey.UserInstalledPlugins: [],
        SystemConfigKey.PluginInstancesImported: True,
    }
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    host = SimpleNamespace(
        reload_plugin=lambda plugin_id: None,
        remove_plugin=lambda plugin_id: None,
        get_plugin_remote_entry=lambda plugin_id, page, version=None: "",
        get_plugins_from_market=lambda market, package_version, force: {},
        async_get_plugins_from_market=lambda *args, **kwargs: {},
        _run_file_watcher=lambda *args, **kwargs: None,
    )
    runtime = build_plugin_runtime(
        host,
        PluginRuntimeEnvironment(
            plugins_root=tmp_path,
            storage=lambda: storage,
            instance_directory=lambda: directory,
            system=lambda: SimpleNamespace(),
            database=lambda: SimpleNamespace(),
            catalog_factory=lambda mapper: SimpleNamespace(),
            import_preparer=lambda **_kwargs: None,
            import_scanner=lambda **_kwargs: None,
            auth_level=lambda: 99,
            remote_entry=host.get_plugin_remote_entry,
            development=lambda: False,
            logger=_logger(),
            multi_version_blockers=lambda *_args, **_kwargs: (),
            set_default_target=lambda source_plugin_id, instance_id: True,
            clear_default_target=lambda source_plugin_id: None,
        ),
        tool_build_max_attempts=1,
    )

    def validator(candidate) -> bool:
        """判定候选类是否具备插件最小生命周期钩子。"""
        return hasattr(candidate, "init_plugin")

    loaded = runtime.lifecycle._load_plugins(None, [], validator, None)

    assert [candidate.__name__ for candidate in loaded] == ["DemoPluginOn"]
    # 停用的分身仍登记在册：卡片与版本绑定视图都要看得见它
    assert set(runtime.instances.all()) == {"DemoPluginOn", "DemoPluginOff"}


def test_runtime_loads_hosts_from_enabled_rows_not_from_the_install_list(tmp_path):
    """本体的装载来源是实例表的启用位，不再是安装清单。

    安装清单只回答「包在不在磁盘上」；它兼任运行开关时，「装着但先不跑」无从表达，
    停用一个本体插件只能靠把它从清单里摘掉——那等于卸载。这里断言的是运行时装出来
    的那个取数端口本身，而不是在测试里复刻一遍它该返回什么。
    """
    from app.runtime.extensions.plugin.runtime import (
        PluginRuntimeEnvironment,
        build_plugin_runtime,
    )

    directory = _make_directory()
    for instance_id in ("DemoPluginOn", "DemoPluginOff"):
        directory.save(
            PluginInstance(
                instance_id=instance_id,
                source_plugin_id=instance_id,
                is_enabled=True,
            )
        )
    assert directory.set_enabled("DemoPluginOff", False) is True

    # 安装清单里两个都在：装载与否已经不看它了
    values = {
        SystemConfigKey.UserInstalledPlugins: ["DemoPluginOn", "DemoPluginOff"],
        SystemConfigKey.PluginInstancesImported: True,
    }
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    host = SimpleNamespace(
        reload_plugin=lambda plugin_id: None,
        remove_plugin=lambda plugin_id: None,
        get_plugin_remote_entry=lambda plugin_id, page, version=None: "",
        get_plugins_from_market=lambda market, package_version, force: {},
        async_get_plugins_from_market=lambda *args, **kwargs: {},
        _run_file_watcher=lambda *args, **kwargs: None,
    )
    runtime = build_plugin_runtime(
        host,
        PluginRuntimeEnvironment(
            plugins_root=tmp_path,
            storage=lambda: storage,
            instance_directory=lambda: directory,
            system=lambda: SimpleNamespace(),
            database=lambda: SimpleNamespace(),
            catalog_factory=lambda mapper: SimpleNamespace(),
            import_preparer=lambda **_kwargs: None,
            import_scanner=lambda **_kwargs: None,
            auth_level=lambda: 99,
            remote_entry=host.get_plugin_remote_entry,
            development=lambda: False,
            logger=_logger(),
            multi_version_blockers=lambda *_args, **_kwargs: (),
            set_default_target=lambda source_plugin_id, instance_id: True,
            clear_default_target=lambda source_plugin_id: None,
        ),
        tool_build_max_attempts=1,
    )

    assert runtime.lifecycle._loadable_plugins() == ["DemoPluginOn"]


def test_clone_allocates_a_suffix_when_the_caller_gives_none():
    """不给后缀时自动分配最小可用序号，从 2 起算。

    后缀只用于区分实例，用户对它无感：逼他想一个既合法又不重复的字符串，只是把
    一个纯粹的内部标识摊到了界面上。
    """
    instances: dict[str, PluginInstance] = {}
    configs = {"DemoPlugin": {"enable": True}}
    service = _restore_service(instances, configs, set(), [])

    success, clone_id = service.clone(plugin_id="DemoPlugin", name="", description="")

    assert success is True
    # 本体在用户眼里就是第 1 个实例，分身接着往下排
    assert clone_id == "DemoPlugin2"


def test_clone_skips_suffixes_already_taken_including_uninstalled_ones():
    """已卸载的分身同样占位：重用它的 ID 会变成恢复，而不是新建一个。"""
    instances: dict[str, PluginInstance] = {
        "DemoPlugin2": PluginInstance(
            instance_id="DemoPlugin2", source_plugin_id="DemoPlugin", is_enabled=True
        ),
        "DemoPlugin3": PluginInstance(
            instance_id="DemoPlugin3", source_plugin_id="DemoPlugin", is_enabled=False
        ),
    }
    configs = {"DemoPlugin": {"enable": True}}
    service = _restore_service(instances, configs, set(), [])

    success, clone_id = service.clone(plugin_id="DemoPlugin", name="", description="")

    assert success is True
    assert clone_id == "DemoPlugin4"


def test_runtime_keeps_uninstalled_clones_out_of_the_installed_catalog(tmp_path):
    """装给插件目录的实例端口只给在册实例。

    目录投影把这个端口的结果直接并进已安装清单；口径若不分启用与否，一个已经
    卸载掉的分身会继续以插件卡片的形式摆在「我的插件」里。这里断言的是运行时
    真正装出来的那个端口，而不是在测试里复刻它该返回什么。
    """
    from app.runtime.extensions.plugin.runtime import (
        PluginRuntimeEnvironment,
        build_plugin_runtime,
    )

    def catalog_factory(_mapper):
        """目录应用服务在本用例里不参与断言。"""
        return SimpleNamespace()

    directory = _make_directory()
    for suffix in ("on", "off"):
        directory.save(
            PluginInstance(
                instance_id=f"DemoPlugin{suffix}",
                source_plugin_id="DemoPlugin",
                is_enabled=True,
            )
        )
    assert directory.set_enabled("DemoPluginoff", False) is True

    values = {
        SystemConfigKey.UserInstalledPlugins: ["DemoPlugin"],
        SystemConfigKey.PluginInstancesImported: True,
    }
    storage = PluginStorage(read=values.get, write=lambda key, value: values.__setitem__(key, value))
    host = SimpleNamespace(
        reload_plugin=lambda plugin_id: None,
        remove_plugin=lambda plugin_id: None,
        get_plugin_remote_entry=lambda plugin_id, page, version=None: "",
        get_plugins_from_market=lambda market, package_version, force: {},
        async_get_plugins_from_market=lambda *args, **kwargs: {},
        _run_file_watcher=lambda *args, **kwargs: None,
    )
    runtime = build_plugin_runtime(
        host,
        PluginRuntimeEnvironment(
            plugins_root=tmp_path,
            storage=lambda: storage,
            instance_directory=lambda: directory,
            system=lambda: SimpleNamespace(),
            database=lambda: SimpleNamespace(),
            catalog_factory=catalog_factory,
            import_preparer=lambda **_kwargs: None,
            import_scanner=lambda **_kwargs: None,
            auth_level=lambda: 99,
            remote_entry=host.get_plugin_remote_entry,
            development=lambda: False,
            logger=_logger(),
            multi_version_blockers=lambda *_args, **_kwargs: (),
            set_default_target=lambda source_plugin_id, instance_id: True,
            clear_default_target=lambda source_plugin_id: None,
        ),
        tool_build_max_attempts=1,
    )
    # 断言目录门面实际持有的那个端口，而不是在测试里复刻它该返回什么
    assert sorted(runtime.catalog._plugin_instances()) == ["DemoPluginon"]
    # 行本身没消失：恢复选择器与版本回收仍要看得见它
    assert sorted(runtime.instances.all()) == ["DemoPluginoff", "DemoPluginon"]
