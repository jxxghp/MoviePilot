"""插件自有数据库的运行时端口、生命周期时机与组合根接线。"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.endpoints.plugin as plugin_endpoint
import app.application.plugin.folders as plugin_folders
import app.application.plugin.management as plugin_management
import app.application.plugin.routes as plugin_routes
import app.application.scheduling as scheduling_module
import app.db.engine as engine_module
import app.db.session as session_module
import app.runtime.extensions.plugin.database as database_module
import app.startup.initializers.plugins as plugins_initializer
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.storage import PluginConfigStore, PluginStorage
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


class ModelA:
    """充当声明模型占位符的哨兵类，仅用于校验实参透传。"""


@pytest.fixture
def restore_plugin_database_port():
    """还原全局插件数据库端口，避免测试间相互污染。"""
    original = database_module.get_plugin_database()
    yield
    database_module.configure_plugin_database(original)


def _recording_database(calls: list[tuple]) -> PluginDatabase:
    """构造把 ensure/release/destroy 三个操作依次记录到同一列表的插件数据库端口。"""
    return PluginDatabase(
        ensure=lambda plugin_id, models, migrations: calls.append(
            ("ensure", plugin_id, models, migrations)
        ),
        release=lambda plugin_id: calls.append(("release", plugin_id)),
        destroy=lambda plugin_id: calls.append(("destroy", plugin_id)),
    )


def _make_plugin_class(
    name: str,
    *,
    models: tuple[type, ...] = (),
    migrations: str | Path | None = None,
    calls: list[tuple] | None = None,
    declare_hooks: bool = True,
) -> type:
    """
    构造一个满足宿主插件最小生命周期合同的类，类名即插件运行时标识。

    分身在真实加载路径中被 ``PluginLoader._adapt_instance_class`` 改写
    ``__name__`` 为 ``instance_id``，因此这里直接用类名模拟分身身份，无需额外维度。
    :param name: 插件运行时标识（即 ``__name__``）
    :param models: ``get_database_models()`` 的返回值
    :param migrations: ``get_database_migrations()`` 的返回值
    :param calls: 记录 ``init_plugin`` 调用的可选列表
    :param declare_hooks: 是否声明数据库钩子；为假时模拟未实现钩子的旧插件
    :return: 满足最小生命周期合同的插件类
    """

    def init_plugin(self, _config):
        """记录初始化调用，供顺序断言使用。"""
        if calls is not None:
            calls.append(("init", name))

    namespace: dict[str, Any] = {
        "plugin_name": name,
        "plugin_version": "1.0.0",
        "init_plugin": init_plugin,
        "get_state": staticmethod(lambda: True),
        "get_name": lambda self: name,
        "close": lambda self: None,
        "stop_service": lambda self: None,
    }
    if declare_hooks:
        namespace["get_database_models"] = lambda self: list(models)
        namespace["get_database_migrations"] = lambda self: migrations
    return type(name, (), namespace)


def _build_lifecycle(**overrides: Any) -> PluginLifecycle:
    """构造直接可用的生命周期实例，默认端口全为空操作，测试按需覆盖。"""
    defaults: dict[str, Any] = dict(
        classes={},
        running={},
        load_plugins=lambda _plugin_id, _installed, _check: [],
        installed_plugins=lambda: [],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: True,
        clear_modules=lambda _plugin_id: None,
        clear_tools=lambda: None,
        enable_events=lambda _plugin: None,
        disable_events=lambda _plugin: None,
        runtime_status_writer=lambda _plugin_id, _status: None,
        database=lambda: PluginDatabase(),
        log=logging.getLogger(__name__),
        event_sender=lambda *_args, **_kwargs: None,
    )
    defaults.update(overrides)
    return PluginLifecycle(**defaults)


def test_default_plugin_database_ports_are_safe_noops(restore_plugin_database_port):
    """未装配组合根时，三个端口方法均是安全的 no-op。"""
    database_module.configure_plugin_database(PluginDatabase())
    database = database_module.get_plugin_database()
    assert database.ensure("demo", (), None) is None
    assert database.release("demo") is None
    assert database.destroy("demo") is None


def test_configure_plugin_database_replaces_the_process_port(restore_plugin_database_port):
    """组合根替换端口后，三次调用按序进入替身记录的调用列表。"""
    calls: list[tuple] = []
    database_module.configure_plugin_database(_recording_database(calls))

    database = database_module.get_plugin_database()
    database.ensure("demo", (), None)
    database.release("demo")
    database.destroy("demo")

    assert [call[0] for call in calls] == ["ensure", "release", "destroy"]


def test_start_ensures_the_database_after_init_plugin():
    """建库发生在 init_plugin 之后，插件桩把两者记到同一 calls 列表验证顺序。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin", calls=calls)
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )

    lifecycle.start("DemoPlugin")

    kinds = [call[0] for call in calls]
    assert kinds.index("init") < kinds.index("ensure")


def test_start_passes_the_declared_models_and_migration_directory():
    """声明的模型与字符串迁移目录原样透传给 ensure，字符串被转换为 Path。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin", models=(ModelA,), migrations="m")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )

    lifecycle.start("DemoPlugin")

    ensure_call = next(call for call in calls if call[0] == "ensure")
    assert ensure_call == ("ensure", "DemoPlugin", (ModelA,), Path("m"))


def test_start_reports_empty_declarations_for_plugins_without_a_database():
    """未实现数据库钩子的插件仍触发 ensure，实参为空元组和 None。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin", declare_hooks=False)
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )

    lifecycle.start("DemoPlugin")

    ensure_call = next(call for call in calls if call[0] == "ensure")
    assert ensure_call == ("ensure", "DemoPlugin", (), None)


def test_plugin_failing_to_ensure_is_not_registered_as_running():
    """建库失败与 init_plugin 抛错同构：进入 load_failed，不进入运行态。"""
    plugin_cls = _make_plugin_class("DemoPlugin")

    def _raise_ensure(_plugin_id, _models, _migrations):
        """模拟建库失败。"""
        raise RuntimeError("ensure failed")

    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: PluginDatabase(ensure=_raise_ensure),
    )

    result = lifecycle.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert "DemoPlugin" not in lifecycle._running


def test_stop_releases_the_database_and_never_destroys_it():
    """停止单个插件只释放连接，不出现任何 destroy 调用。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )
    lifecycle.start("DemoPlugin")

    lifecycle.stop("DemoPlugin")

    assert ("release", "DemoPlugin") in calls
    assert not any(call[0] == "destroy" for call in calls)


def test_stop_without_plugin_id_releases_every_running_plugin():
    """整体停止时，每个运行中的插件都收到一次 release。"""
    calls: list[tuple] = []
    plugin_a = _make_plugin_class("PluginA")
    plugin_b = _make_plugin_class("PluginB")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_a, plugin_b],
        installed_plugins=lambda: ["PluginA", "PluginB"],
        database=lambda: _recording_database(calls),
    )
    lifecycle.start()

    lifecycle.stop()

    released = {call[1] for call in calls if call[0] == "release"}
    assert released == {"PluginA", "PluginB"}


def test_reload_releases_then_ensures_again():
    """热重载先释放旧连接再重新建库，全程不出现 destroy。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )
    lifecycle.start("DemoPlugin")
    calls.clear()

    lifecycle.reload("DemoPlugin", "plugin-reload")

    kinds = [call[0] for call in calls if call[0] in ("release", "ensure", "destroy")]
    assert kinds.index("release") < kinds.index("ensure")
    assert "destroy" not in kinds


def test_release_failure_does_not_block_unloading():
    """release 抛异常时卸载仍收敛完成，插件从运行态移出。"""
    plugin_cls = _make_plugin_class("DemoPlugin")

    def _raise_release(_plugin_id):
        """模拟释放连接失败。"""
        raise RuntimeError("release failed")

    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: PluginDatabase(release=_raise_release),
    )
    lifecycle.start("DemoPlugin")

    assert lifecycle.quiesce("DemoPlugin") is True
    assert lifecycle.finalize("DemoPlugin") is True
    assert "DemoPlugin" not in lifecycle._running


def test_delete_plugin_data_destroys_the_plugin_database():
    """重置数据先删宿主业务数据行，再销毁插件自有库。"""
    calls: list[tuple] = []
    store = PluginConfigStore(
        storage=lambda: PluginStorage(
            delete_data=lambda plugin_id: calls.append(("storage.delete_data", plugin_id))
        ),
        database=lambda: _recording_database(calls),
        plugin_exists=lambda _plugin_id: True,
    )

    result = store.delete_data("DemoPlugin", force=True)

    assert result is True
    kinds = [call[0] for call in calls]
    assert kinds.index("storage.delete_data") < kinds.index("destroy")
    assert ("destroy", "DemoPlugin") in calls


def test_delete_plugin_data_without_force_refuses_unknown_plugin():
    """未知插件且非强制删除时拒绝执行，不触发销毁。"""
    calls: list[tuple] = []
    store = PluginConfigStore(
        storage=lambda: PluginStorage(
            delete_data=lambda plugin_id: calls.append(("storage.delete_data", plugin_id))
        ),
        database=lambda: _recording_database(calls),
        plugin_exists=lambda _plugin_id: False,
    )

    result = store.delete_data("DemoPlugin", force=False)

    assert result is False
    assert calls == []


def test_clone_uninstall_destroys_only_the_clone_database():
    """分身卸载只销毁分身自己的库，单键语义下与源插件互不影响。"""
    calls: list[tuple] = []
    store = PluginConfigStore(
        storage=lambda: PluginStorage(
            delete_data=lambda plugin_id: calls.append(("storage.delete_data", plugin_id))
        ),
        database=lambda: _recording_database(calls),
        plugin_exists=lambda _plugin_id: True,
    )

    store.delete_data("DemoPluginwork", force=True)

    destroyed = [call[1] for call in calls if call[0] == "destroy"]
    assert destroyed == ["DemoPluginwork"]


def test_remove_plugin_only_releases_the_database():
    """从内存移除插件的内部路径即 stop，只释放不销毁。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )
    lifecycle.start("DemoPlugin")

    lifecycle.stop("DemoPlugin")

    assert ("release", "DemoPlugin") in calls
    assert not any(call[0] == "destroy" for call in calls)


def test_close_database_releases_plugin_databases_before_the_host_engine(monkeypatch):
    """进程关停时先释放插件库，再释放宿主同步引擎。"""
    calls: list[str] = []
    monkeypatch.setattr(session_module, "release_all_databases", lambda: calls.append("plugins"))
    fake_sync_engine = MagicMock()
    fake_sync_engine.dispose.side_effect = lambda: calls.append("sync engine")
    monkeypatch.setattr(engine_module, "peek_sync_engine", lambda: fake_sync_engine)
    monkeypatch.setattr(engine_module, "peek_async_engine", lambda: None)
    monkeypatch.setattr(session_module, "_pooled_async_engines", {})

    asyncio.run(session_module.close_database())

    assert calls == ["plugins", "sync engine"]


def test_startup_composition_binds_the_plugin_database_to_the_db_framework(monkeypatch):
    """组合根把插件数据库端口装配到 db 层的建库、释放与销毁实现。"""
    calls: list[tuple] = []
    monkeypatch.setattr(
        plugins_initializer,
        "ensure_database",
        lambda plugin_id, models, migrations: calls.append(("ensure", plugin_id)),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "release_database",
        lambda plugin_id: calls.append(("release", plugin_id)),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "destroy_database",
        lambda plugin_id: calls.append(("destroy", plugin_id)),
    )

    database = plugins_initializer._build_plugin_database()
    database.ensure("demo", (), None)
    database.release("demo")
    database.destroy("demo")

    assert [call[0] for call in calls] == ["ensure", "release", "destroy"]


def test_plugin_runtime_database_port_does_not_import_the_database_layer():
    """端口模块的 import 语句里不含任何 app.db 前缀，保证运行时不依赖数据库实现。"""
    source_path = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "runtime"
        / "extensions"
        / "plugin"
        / "database.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(module.startswith("app.db") for module in imported_modules)


def test_start_releases_the_database_of_a_plugin_that_failed_to_load():
    """建库失败的插件不会进入运行态，其句柄在失败分支就地释放。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")

    def _raise_ensure(plugin_id, _models, _migrations):
        """模拟建库失败，但句柄可能已经建立。"""
        calls.append(("ensure", plugin_id))
        raise RuntimeError("ensure failed")

    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: PluginDatabase(
            ensure=_raise_ensure,
            release=lambda plugin_id: calls.append(("release", plugin_id)),
        ),
    )

    lifecycle.start("DemoPlugin")

    assert [call for call in calls if call[0] == "release"] == [("release", "DemoPlugin")]
    assert "DemoPlugin" not in lifecycle._running


def test_stop_all_releases_plugins_that_never_reached_the_running_registry():
    """整体停止会释放启动中途失败、只登记在类注册表里的插件。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")

    def _raise_ensure(_plugin_id, _models, _migrations):
        """模拟建库失败。"""
        raise RuntimeError("ensure failed")

    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: PluginDatabase(
            ensure=_raise_ensure,
            release=lambda plugin_id: calls.append(("release", plugin_id)),
        ),
    )
    lifecycle.start()
    assert "DemoPlugin" not in lifecycle._running
    assert "DemoPlugin" in lifecycle._classes
    calls.clear()

    lifecycle.stop()

    assert ("release", "DemoPlugin") in calls


def test_stopping_an_already_stopped_plugin_stays_idempotent():
    """卸载编排先停后删，删除之后的 remove_plugin 会再停一次，二次停止必须无害。"""
    calls: list[tuple] = []
    plugin_cls = _make_plugin_class("DemoPlugin")
    lifecycle = _build_lifecycle(
        load_plugins=lambda *_a, **_kw: [plugin_cls],
        installed_plugins=lambda: ["DemoPlugin"],
        database=lambda: _recording_database(calls),
    )
    lifecycle.start("DemoPlugin")
    lifecycle.stop("DemoPlugin")
    calls.clear()

    lifecycle.stop("DemoPlugin")

    assert calls == [("release", "DemoPlugin")]
    assert lifecycle._classes == {}
    assert lifecycle._running == {}


def _uninstall_manager() -> MagicMock:
    """构造卸载分身所需的插件管理器替身，按调用顺序记录全部方法调用。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_instance.return_value = None
    plugin_manager.get_plugin_source_instances.return_value = []
    plugin_manager.plugins = {"DemoPluginwork": MagicMock(is_clone=True)}
    plugin_manager.remove_plugin_package.return_value = True
    return plugin_manager


def _assert_stop_precedes_deletion(plugin_manager: MagicMock) -> None:
    """断言插件先停止再删除数据，且删除按 force 执行。"""
    method_names = [name for name, _args, _kwargs in plugin_manager.mock_calls]
    assert method_names.index("stop") < method_names.index("delete_plugin_config")
    assert method_names.index("stop") < method_names.index("delete_plugin_data")
    plugin_manager.stop.assert_called_once_with("DemoPluginwork")
    plugin_manager.delete_plugin_config.assert_called_once_with(
        "DemoPluginwork",
        force=True,
    )
    plugin_manager.delete_plugin_data.assert_called_once_with(
        "DemoPluginwork",
        force=True,
    )


def test_http_uninstall_stops_the_plugin_before_deleting_its_data(monkeypatch):
    """HTTP 卸载先停插件再删数据，停机钩子因此无法重建刚销毁的自有库。"""
    plugin_manager = _uninstall_manager()
    config = MagicMock()
    config.get.return_value = ["DemoPluginwork"]
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_api", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_job", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_from_folders", MagicMock())

    result = plugin_endpoint.uninstall_plugin("DemoPluginwork", None)

    assert result.success is True
    _assert_stop_precedes_deletion(plugin_manager)


def test_runtime_uninstall_stops_the_plugin_before_deleting_its_data(monkeypatch):
    """运行态卸载编排同样先停后删，两条卸载路径的数据删除时机保持一致。"""
    plugin_manager = _uninstall_manager()
    config = MagicMock()
    config.get.return_value = ["DemoPluginwork"]
    config.async_set = AsyncMock()
    monkeypatch.setattr(plugin_management, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(
        plugin_management,
        "get_configured_system_config",
        lambda: config,
    )
    monkeypatch.setattr(plugin_routes, "remove_plugin_api", MagicMock())
    monkeypatch.setattr(scheduling_module, "remove_plugin_job", MagicMock())
    monkeypatch.setattr(plugin_folders, "remove_plugin_from_folders", MagicMock())

    result = asyncio.run(plugin_management.uninstall_plugin_runtime("DemoPluginwork"))

    assert result == {"was_clone": True, "clone_files_removed": True}
    _assert_stop_precedes_deletion(plugin_manager)


def test_uninstall_virtual_instance_also_stops_before_deleting(monkeypatch):
    """虚拟实例卸载同样先停后删，force 删除不受插件类注销影响。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_instance.return_value = PluginInstance(
        instance_id="DemoPluginwork",
        source_plugin_id="DemoPlugin",
    )
    plugin_manager.get_plugin_source_instances.return_value = []
    plugin_manager.plugins = {}
    config = MagicMock()
    config.get.return_value = ["DemoPlugin"]
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_api", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_job", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_from_folders", MagicMock())

    result = plugin_endpoint.uninstall_plugin("DemoPluginwork", None)

    assert result.success is True
    config.set.assert_called_once_with(
        SystemConfigKey.UserInstalledPlugins,
        ["DemoPlugin"],
    )
    _assert_stop_precedes_deletion(plugin_manager)
