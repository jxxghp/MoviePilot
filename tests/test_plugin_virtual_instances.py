"""虚拟插件实例的持久化、加载和创建行为测试。"""

import importlib.util
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.application.plugin.management as plugin_management
from app.runtime.event.registry import EventRegistry
from app.runtime.extensions.plugin.clone import PluginCloneService
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import EventType, SystemConfigKey


def _make_directory() -> tuple[PluginInstanceDirectory, dict[str, PluginInstance]]:
    """构造进程内插件实例表，供分身持久化测试使用。

    返回背后的字典，用例据此断言写入落在哪一行上，而不是只看端口回报的结果。
    """
    records: dict[str, PluginInstance] = {}

    directory = PluginInstanceDirectory(
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
    return directory, records


def _make_storage(values: dict) -> tuple[PluginStorage, list]:
    """构造进程内配置存储，并记录写入过的键，供幂等性断言使用。"""
    written: list = []

    def _write(key, value):
        """记录写入键并落进内存配置字典。"""
        written.append(key)
        values[key] = value

    return PluginStorage(read=values.get, write=_write), written


def _legacy_values(**entries) -> dict:
    """构造只含旧 systemconfig 单键的配置字典。"""
    return {SystemConfigKey.PluginInstances: dict(entries)}


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
    directory, _records = _make_directory()
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


def test_host_rows_do_not_leak_into_the_clone_listing():
    """本体自身那一行与分身共用一张表，但不得出现在分身清单里。

    本体行承载的是插件自己的业务参数，混进分身清单会让「我的插件」里多出一张
    指向插件自身的分身卡片。
    """
    values = {SystemConfigKey.UserInstalledPlugins: ["DemoPlugin"]}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory, records = _make_directory()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    store.save(clone)
    records["DemoPlugin"] = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
    )

    assert store.all() == {"DemoPluginWork": clone}
    assert store.for_source("DemoPlugin") == [clone]
    assert store.get("DemoPlugin") is None
    assert store.delete("DemoPlugin") is False
    assert "DemoPlugin" in records


def test_saving_a_clone_whose_id_equals_its_source_is_rejected():
    """两者相等的那一行表示本体自身，按分身写入会把本体承载的配置顶掉。"""
    values: dict = {}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory, records = _make_directory()
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    with pytest.raises(ValueError):
        store.save(
            PluginInstance(instance_id="DemoPlugin", source_plugin_id="DemoPlugin")
        )

    assert records == {}


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
    directory, _records = _make_directory()

    # 首次访问：旧键内容被导入独立表
    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork"}

    # 用户删光全部分身，表重新变空
    assert first.delete("DemoPluginWork") is True
    assert first.all() == {}

    # 进程重启：新建 store 重新走一次兜底导入判定
    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert second.all() == {}
    assert values[SystemConfigKey.PluginInstances] is not None


def test_legacy_entries_added_by_an_older_version_are_merged_in():
    """回滚到旧版本新增的分身，切回新版本后要能合并进独立表。

    旧版本只认 `PluginInstances` 旧键，在那里新建的分身既不在独立表里，也不在
    已导入指纹里。若判据只有「一条永久标记」加「整表是否为空」，标记落下且表非空
    之后这些分身会被永久跳过，保留旧键作回滚依据的承诺就落空了。
    """
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    # 新版本首次启动：旧键内容导入独立表，落下已导入指纹
    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork"}
    assert values[SystemConfigKey.PluginInstancesImported]

    # 回滚到旧版本：旧版本只往旧键里新建分身，独立表保持不动
    values[SystemConfigKey.PluginInstances]["DemoPluginHome"] = {
        "instance_id": "DemoPluginHome",
        "source_plugin_id": "DemoPlugin",
        "plugin_name": "家庭实例",
    }

    # 切回新版本
    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert set(second.all()) == {"DemoPluginWork", "DemoPluginHome"}
    assert records["DemoPluginHome"].plugin_name == "家庭实例"


def test_legacy_entries_changed_by_an_older_version_are_merged_in():
    """回滚到旧版本改写过的分身，切回新版本后要按旧键的新内容合并。"""
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
            "plugin_name": "工作实例",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert first.all()["DemoPluginWork"].plugin_name == "工作实例"

    values[SystemConfigKey.PluginInstances]["DemoPluginWork"]["plugin_name"] = "旧版本改过"

    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert second.all()["DemoPluginWork"].plugin_name == "旧版本改过"
    assert records["DemoPluginWork"].plugin_name == "旧版本改过"


def test_rows_edited_in_the_new_table_are_not_overwritten_by_stale_legacy_copies():
    """旧键没变时，独立表里被用户改过的行不得被旧键里的陈旧副本盖回去。"""
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
            "plugin_name": "工作实例",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork"}

    # 用户在新版本里改名，旧键始终没人动过
    records["DemoPluginWork"] = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_name="用户改过",
    )

    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert second.all()["DemoPluginWork"].plugin_name == "用户改过"


def test_rows_migrated_by_alembic_are_claimed_without_being_overwritten():
    """迁移已搬进独立表、指纹还没记过的那批行，只认领指纹不覆盖内容。"""
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
            "plugin_name": "工作实例",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()
    # 模拟 alembic 迁移搬完之后用户又改过名
    records["DemoPluginWork"] = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_name="迁移后改过",
    )

    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert store.all()["DemoPluginWork"].plugin_name == "迁移后改过"
    assert set(values[SystemConfigKey.PluginInstancesImported]) == {"DemoPluginWork"}


def test_merging_legacy_entries_is_idempotent_across_restarts():
    """旧键没有新增或变化时，重复启动既不重复建行也不重复落盘指纹。"""
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
        }
    )
    storage, written = _make_storage(values)
    directory, records = _make_directory()

    for _restart in range(3):
        store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
        assert set(store.all()) == {"DemoPluginWork"}

    assert set(records) == {"DemoPluginWork"}
    assert written == [SystemConfigKey.PluginInstancesImported]


def test_entries_deleted_by_an_older_version_are_removed_from_the_table():
    """回滚到旧版本删掉的分身，切回新版本后必须从独立表里一并消失。

    旧键是旧版本唯一认得的清单，保留它作回滚依据就等于承诺「在那边做的改动切回来
    还算数」。只合并新增与改写、不同步删除，用户在旧版本里删掉的分身会在新版本里
    继续出现并继续被装载。
    """
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
        },
        DemoPluginHome={
            "instance_id": "DemoPluginHome",
            "source_plugin_id": "DemoPlugin",
        },
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork", "DemoPluginHome"}

    # 回滚到旧版本：旧版本只认旧键，用户在那里删掉了一个分身
    del values[SystemConfigKey.PluginInstances]["DemoPluginHome"]

    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert set(second.all()) == {"DemoPluginWork"}
    assert set(records) == {"DemoPluginWork"}
    assert set(values[SystemConfigKey.PluginInstancesImported]) == {"DemoPluginWork"}


def test_renaming_in_the_legacy_key_does_not_leave_the_old_row_behind():
    """旧版本里的重命名表现为「旧 ID 消失 + 新 ID 出现」，旧行不得残留。"""
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
            "plugin_name": "工作实例",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    assert set(first.all()) == {"DemoPluginWork"}

    values[SystemConfigKey.PluginInstances] = {
        "DemoPluginHome": {
            "instance_id": "DemoPluginHome",
            "source_plugin_id": "DemoPlugin",
            "plugin_name": "工作实例",
        }
    }

    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert set(second.all()) == {"DemoPluginHome"}
    assert set(records) == {"DemoPluginHome"}


def test_rows_created_natively_in_the_new_table_survive_legacy_deletions():
    """新表里原生创建、从未由旧键导入过的分身不受旧键删除牵连。

    删除判据必须是「曾经认领过指纹、现在旧键里没有了」；若退化成「旧键里没有就删」，
    新版本创建的分身会在下一次启动时被旧键当成「已删除」整批清掉。
    """
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
        }
    )
    storage, _written = _make_storage(values)
    directory, records = _make_directory()

    first = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)
    first.save(
        PluginInstance(
            instance_id="DemoPluginNative",
            source_plugin_id="DemoPlugin",
        )
    )
    assert set(first.all()) == {"DemoPluginWork", "DemoPluginNative"}

    # 旧版本删掉它自己那条，原生创建的那条与旧键无关
    del values[SystemConfigKey.PluginInstances]["DemoPluginWork"]

    second = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    assert set(second.all()) == {"DemoPluginNative"}
    assert set(records) == {"DemoPluginNative"}


def test_bootstrap_is_retried_after_a_transient_persistence_failure():
    """引导过程中的暂时性故障不得让同一进程此后永久跳过引导。

    完成标记若在读取、合并、写指纹之前就落下，首次访问撞上一次数据库抖动之后，
    本进程剩余生命周期里读到的都是一份没导完的分身清单，只能靠重启自愈。
    """
    values = _legacy_values(
        DemoPluginWork={
            "instance_id": "DemoPluginWork",
            "source_plugin_id": "DemoPlugin",
        }
    )
    directory, _records = _make_directory()
    reads: list = []
    failures = {"remaining": 1}

    def _read(key):
        """第一次读旧键时模拟一次持久化层抖动。"""
        reads.append(key)
        if key is SystemConfigKey.PluginInstances and failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("持久化层暂时不可用")
        return values.get(key)

    storage = PluginStorage(
        read=_read,
        write=lambda key, value: values.__setitem__(key, value),
    )
    store = PluginInstanceStore(storage=lambda: storage, directory=lambda: directory)

    with pytest.raises(RuntimeError):
        store.all()

    assert set(store.all()) == {"DemoPluginWork"}

    # 成功之后仍然是一次性的：后续读取不再回头查旧键
    reads_after_success = len(reads)
    assert set(store.all()) == {"DemoPluginWork"}
    assert len(reads) == reads_after_success


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


def _import_host_package(module_name: str, source_dir: Path) -> ModuleType:
    """按源插件本体的模块名导入磁盘源码，复刻本体已被装载的运行事实。

    本体必须真的在 ``sys.modules`` 里：处理器的注册键要靠函数的 ``__module__``
    反查模块对象才能算出来，本体缺席会让两边都算成同一个占位名，掩盖分身与本体
    撞键这件事本身。
    """
    spec = importlib.util.spec_from_file_location(
        module_name,
        source_dir / "__init__.py",
        submodule_search_locations=[str(source_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _forget_modules(*prefixes: str) -> None:
    """清除用例在模块缓存里留下的插件模块，避免污染后续用例。"""
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(name, None)


def test_loader_gives_submodule_handlers_of_each_instance_distinct_registry_keys(
    tmp_path,
    monkeypatch,
):
    """分身在子模块类体内声明的处理器，注册键必须与本体的区分开。

    事件处理器的注册键取自函数所在模块名。类体内的函数是类的属性而不是模块的
    属性，只遍历模块顶层碰不到它们；漏改会让分身与本体注册到同一个键上，后注册
    的顶掉先注册的：分身收不到事件，停掉本体会把分身一起停掉。
    """
    source_dir = tmp_path / "identityplugin"
    source_dir.mkdir()
    (source_dir / "core.py").write_text(
        "class IdentityHandlers:\n"
        "    def on_event(self, event):\n"
        "        return event\n"
        "\n"
        "    @staticmethod\n"
        "    def on_static_event(event):\n"
        "        return event\n",
        encoding="utf-8",
    )
    (source_dir / "__init__.py").write_text(
        "from app.plugins.identityplugin.core import IdentityHandlers\n"
        "\n"
        "\n"
        "class IdentityPlugin(IdentityHandlers):\n"
        "    plugin_name = 'Identity'\n"
        "\n"
        "    def init_plugin(self, _config):\n"
        "        return None\n",
        encoding="utf-8",
    )
    import app.plugins as plugin_package

    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
    )
    try:
        host_module = _import_host_package("app.plugins.identityplugin", source_dir)
        monkeypatch.setattr(
            plugin_package, "identityplugin", host_module, raising=False
        )
        clone_class = loader.load_instance(
            PluginInstance(
                instance_id="IdentityPluginWork",
                source_plugin_id="IdentityPlugin",
            ),
            lambda candidate: hasattr(candidate, "init_plugin"),
        )[0]
        host_handler = host_module.IdentityPlugin.on_event
        clone_handler = clone_class.on_event
        subscribers: dict = {}
        registry = EventRegistry(
            lock=threading.RLock(),
            broadcast_subscribers=lambda: subscribers,
            chain_subscribers=dict,
            disabled_handlers=set,
            disabled_classes=set,
        )
        registry.add(EventType.PluginAction, host_handler, 0)
        registry.add(EventType.PluginAction, clone_handler, 0)

        assert len(subscribers[EventType.PluginAction]) == 2
        assert EventRegistry.handler_identifier(host_handler) == (
            "app.plugins.identityplugin.core.IdentityHandlers.on_event"
        )
        assert EventRegistry.handler_identifier(clone_handler) == (
            "app.plugins.identitypluginwork.core.IdentityHandlers.on_event"
        )
        assert EventRegistry.handler_identifier(clone_class.on_static_event) == (
            "app.plugins.identitypluginwork.core.IdentityHandlers.on_static_event"
        )
    finally:
        _forget_modules(
            "app.plugins.identityplugin",
            "app.plugins.identitypluginwork",
        )


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
        instance_id_taken=lambda plugin_id: plugin_id in instances,
        get_instance=instances.get,
        source_plugin_id=lambda plugin_id: plugin_id,
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        delete_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        disable_instance=lambda plugin_id: plugin_id in instances,
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
        instance_id_taken=lambda plugin_id: plugin_id in instances,
        get_instance=instances.get,
        source_plugin_id=lambda plugin_id: plugin_id,
        save_instance=lambda instance: instances.__setitem__(
            instance.instance_id,
            instance,
        ),
        delete_instance=lambda plugin_id: instances.pop(plugin_id, None) is not None,
        disable_instance=lambda plugin_id: plugin_id in instances,
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


def _tree_manager(reloaded: list[str]) -> MagicMock:
    """构造带一个分身的插件管理器替身，重载树语义取自真实实现。

    只替换重载动作本身，实例树的解析仍走 PluginManager 的真实方法，避免用例把
    「分身也被重载」断言在一个自己编造的树上。
    """
    directory, records = _make_directory()
    records["DemoPluginwork"] = PluginInstance(
        instance_id="DemoPluginwork",
        source_plugin_id="DemoPlugin",
        plugin_name="工作实例",
    )
    storage, _written = _make_storage({})
    manager = MagicMock()
    manager._plugin_instance_store = PluginInstanceStore(
        storage=lambda: storage,
        directory=lambda: directory,
    )
    manager._plugin_quiesce_lock = threading.RLock()
    manager.mutation.side_effect = lambda _operation: nullcontext()
    manager.reload_plugin.side_effect = lambda plugin_id: (
        reloaded.append(plugin_id) or PluginRuntimeStatus.ACTIVE
    )
    manager.get_plugin_source_id.side_effect = lambda plugin_id: (
        PluginManager.get_plugin_source_id(manager, plugin_id)
    )
    manager.reload_plugin_tree.side_effect = lambda plugin_id: (
        PluginManager.reload_plugin_tree(manager, plugin_id)
    )
    manager.get_plugin_reload_targets.side_effect = lambda plugin_id: (
        PluginManager.get_plugin_reload_targets(manager, plugin_id)
    )
    return manager


def test_manual_reload_covers_source_plugin_and_its_clones(monkeypatch):
    """手工重载本体要连分身一起换掉旧类对象，并逐个刷新它们的注册。

    只重载本体时，分身继续持有旧模块与旧类对象，改完源码点重载后新旧代码会在同一
    进程内并存，分身的 API、调度与命令注册也不会刷新。
    """
    reloaded: list[str] = []
    refreshed: list[str] = []
    manager = _tree_manager(reloaded)
    monkeypatch.setattr(plugin_management, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugin_management,
        "refresh_plugin_registrations",
        refreshed.append,
    )

    status = plugin_management.reload_plugin_runtime("DemoPlugin")

    assert status is PluginRuntimeStatus.ACTIVE
    assert reloaded == ["DemoPlugin", "DemoPluginwork"]
    assert refreshed == ["DemoPlugin", "DemoPluginwork"]


def test_manual_reload_of_a_clone_rebuilds_the_whole_instance_tree(monkeypatch):
    """从分身发起的重载要回到源码本体，再带上同源的全部分身。"""
    reloaded: list[str] = []
    refreshed: list[str] = []
    manager = _tree_manager(reloaded)
    monkeypatch.setattr(plugin_management, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugin_management,
        "refresh_plugin_registrations",
        refreshed.append,
    )

    status = plugin_management.reload_plugin_runtime("DemoPluginwork")

    assert status is PluginRuntimeStatus.ACTIVE
    assert reloaded == ["DemoPlugin", "DemoPluginwork"]
    assert refreshed == ["DemoPlugin", "DemoPluginwork"]
