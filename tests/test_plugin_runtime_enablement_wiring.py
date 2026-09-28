"""插件运行时组合根的启用语义接线测试：定向装载取数与默认目标存在性判据。

这两件事都只在 ``build_plugin_runtime`` 里成立：各能力类自己只认注入进来的端口，
端口接到哪一个判据是组合根的决定，因而只能在这一层验证。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.runtime import (
    PluginRuntime,
    PluginRuntimeEnvironment,
    build_plugin_runtime,
)
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginStorage,
)
from app.runtime.log import (
    clear_plugin_instance_log_level,
    get_effective_plugin_instance_log_level,
)
from app.schemas.plugin import PluginInstance
from app.schemas.types import SystemConfigKey


def _logger() -> SimpleNamespace:
    """提供运行时构造所需的最小日志对象。"""
    return SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )


def _host() -> SimpleNamespace:
    """提供运行时构造所需的最小宿主门面。"""
    return SimpleNamespace(
        reload_plugin=lambda _plugin_id: None,
        remove_plugin=lambda _plugin_id: None,
        get_plugin_remote_entry=lambda _plugin_id, _page: "",
        _run_file_watcher=lambda: None,
        get_plugins_from_market=lambda *_args, **_kwargs: None,
        async_get_plugins_from_market=lambda *_args, **_kwargs: None,
    )


def _build_runtime(
    *,
    records: dict[str, PluginInstance] | None = None,
    installed: list[str] | None = None,
) -> tuple[PluginRuntime, dict[str, PluginInstance]]:
    """按给定实例行与安装清单装配一个全内存的插件运行时。

    :param records: 实例表初始行，键为实例 ID
    :param installed: ``UserInstalledPlugins`` 安装清单
    :return: 运行时与其背后的实例表字典
    """
    rows: dict[str, PluginInstance] = dict(records or {})
    values: dict = {SystemConfigKey.UserInstalledPlugins: list(installed or [])}
    storage = PluginStorage(
        read=values.get,
        write=lambda key, value: values.__setitem__(key, value),
    )
    directory = PluginInstanceDirectory(
        get=rows.get,
        list_all=lambda: list(rows.values()),
        list_by_source=lambda source_plugin_id: [
            record
            for record in rows.values()
            if record.source_plugin_id == source_plugin_id
        ],
        save=lambda instance: rows.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: rows.pop(instance_id, None) is not None,
        list_enabled=lambda: [record for record in rows.values() if record.is_enabled],
        set_enabled=lambda instance_id, is_enabled: instance_id in rows,
    )
    environment = PluginRuntimeEnvironment(
        plugins_root=Path("/nonexistent-plugins-root"),
        storage=lambda: storage,
        instance_directory=lambda: directory,
        system=lambda: SimpleNamespace(),
        database=lambda: SimpleNamespace(),
        catalog_factory=lambda _mapper: SimpleNamespace(),
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        auth_level=lambda: 0,
        remote_entry=lambda _plugin_id, _page: "",
        development=lambda: False,
        logger=_logger(),
        set_default_target=lambda _plugin_id, _instance_id: True,
        clear_default_target=lambda _plugin_id: None,
        runtime_declaration=lambda _plugin_id: {},
    )
    runtime = build_plugin_runtime(
        _host(),
        environment,
        tool_build_max_attempts=1,
    )
    return runtime, rows


# --------------------------------------------------------------------------- #
# 默认调用目标：存在性判据不得绑定在运行期类注册表上
# --------------------------------------------------------------------------- #


def test_default_target_management_survives_all_instances_being_disabled():
    """插件全部实例停用并重启后，默认目标置位接口仍要认得这个插件。

    启动只把启用中的本体与分身装进运行期类注册表，把存在性绑在注册表上就等于说
    「停用即不存在」；但它的安装记录与实例行都还在，停用不是卸载，在册的实例必须
    仍然可见、可管理。
    """
    runtime, _rows = _build_runtime(
        records={
            "DemoPlugin": PluginInstance(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                is_enabled=False,
            )
        },
        installed=["DemoPlugin"],
    )
    # 前置事实：停用的插件没有被装载，运行期类注册表里查不到它
    assert runtime.registry.plugin_class("DemoPlugin") is None

    assert runtime.default_target.set_target("DemoPlugin", "DemoPlugin") is True
    runtime.default_target.clear_target("DemoPlugin", "DemoPlugin")


def test_default_target_management_accepts_an_installed_plugin_without_any_row():
    """只有安装记录、还没有任何实例行的插件同样算在册。"""
    runtime, _rows = _build_runtime(records={}, installed=["DemoPlugin"])

    assert runtime.default_target.set_target("DemoPlugin", "DemoPlugin") is True


def test_default_target_management_still_rejects_a_plugin_that_is_not_installed():
    """既没装过、也没有任何实例行的插件仍要按不存在拒绝。"""
    runtime, _rows = _build_runtime(records={}, installed=[])

    with pytest.raises(LookupError):
        runtime.default_target.set_target("GhostPlugin", "GhostPlugin")


# --------------------------------------------------------------------------- #
# 定向装载：带具体实例 ID 的装载同样要认启用位
# --------------------------------------------------------------------------- #


def test_targeted_load_does_not_start_a_disabled_clone(monkeypatch):
    """定向重载一个停用的分身不得把它重新拉起来。

    源码变更触发的实例树重载会带着每个分身的实例 ID 走这条路；实例存储的读取口
    刻意返回全部在册行（含停用的），这条路不自己认启用位，用户停用的分身就会在
    下一次源码变更时又开始跑。
    """
    runtime, _rows = _build_runtime(
        records={
            "DemoPluginWork": PluginInstance(
                instance_id="DemoPluginWork",
                source_plugin_id="DemoPlugin",
                is_enabled=False,
            )
        },
        installed=["DemoPlugin"],
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        runtime.loader,
        "load_instance",
        lambda instance, _validator: loaded.append(instance.instance_id) or [],
    )

    runtime.lifecycle.start("DemoPluginWork")

    assert loaded == []


def test_targeted_load_still_starts_an_enabled_clone(monkeypatch):
    """启用中的分身仍按原路装载，启用位不得把正常重载一并挡掉。"""
    runtime, _rows = _build_runtime(
        records={
            "DemoPluginWork": PluginInstance(
                instance_id="DemoPluginWork",
                source_plugin_id="DemoPlugin",
                is_enabled=True,
            )
        },
        installed=["DemoPlugin"],
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        runtime.loader,
        "load_instance",
        lambda instance, _validator: loaded.append(instance.instance_id) or [],
    )

    runtime.lifecycle.start("DemoPluginWork")

    assert loaded == ["DemoPluginWork"]


def test_targeted_load_does_not_start_a_disabled_host(monkeypatch):
    """定向重载一个停用的本体同样不得把它重新拉起来。

    加载器在收到具体插件 ID 时只按这个 ID 找目录，完全不看传进来的可装载清单；
    本体的装载判据搬到启用位之后，这条路就绕开了那个判据。
    """
    runtime, _rows = _build_runtime(
        records={
            "DemoPlugin": PluginInstance(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                is_enabled=False,
            )
        },
        installed=["DemoPlugin"],
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        runtime.loader,
        "load",
        lambda plugin_id, _loadable, _validator: loaded.append(plugin_id) or [],
    )

    runtime.lifecycle.start("DemoPlugin")

    assert loaded == []


def test_targeted_load_still_starts_an_enabled_host(monkeypatch):
    """启用中的本体仍按原路装载。"""
    runtime, _rows = _build_runtime(
        records={
            "DemoPlugin": PluginInstance(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                is_enabled=True,
            )
        },
        installed=["DemoPlugin"],
    )
    loaded: list[str] = []
    monkeypatch.setattr(
        runtime.loader,
        "load",
        lambda plugin_id, _loadable, _validator: loaded.append(plugin_id) or [],
    )

    runtime.lifecycle.start("DemoPlugin")

    assert loaded == ["DemoPlugin"]


# --------------------------------------------------------------------------- #
# 实例日志等级：存在性判据与默认调用目标同源，同样不得绑定在类注册表上
# --------------------------------------------------------------------------- #


def test_log_level_management_survives_all_instances_being_disabled():
    """插件全部实例停用并重启后，日志等级仍要可查询、可设置。

    与默认调用目标同一个问题：这两个接口问的都是「这个插件还在不在册、能不能被
    管理」，不是「它此刻装载了没有」。绑在运行期类注册表上，用户停用一个插件之后
    就再也调不出它的日志等级设置，而那份设置正是排查它为什么被停用时要看的。
    """
    runtime, _rows = _build_runtime(
        records={
            "DemoPlugin": PluginInstance(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                is_enabled=False,
            )
        },
        installed=["DemoPlugin"],
    )
    # 前置事实：停用的插件没有被装载，运行期类注册表里查不到它
    assert runtime.registry.plugin_class("DemoPlugin") is None

    try:
        levels = runtime.log_level.list_levels("DemoPlugin")
        assert [entry["instance_id"] for entry in levels] == ["DemoPlugin"]

        runtime.log_level.set_level("DemoPlugin", "DemoPlugin", "DEBUG")
        assert get_effective_plugin_instance_log_level("DemoPlugin") == "DEBUG"

        runtime.log_level.clear_level("DemoPlugin", "DemoPlugin")
    finally:
        # 覆盖缓存是进程级状态，用例自己收干净
        clear_plugin_instance_log_level("DemoPlugin")


def test_log_level_management_still_rejects_a_plugin_that_is_not_installed():
    """既没装过、也没有任何实例行的插件仍要按不存在拒绝。"""
    runtime, _rows = _build_runtime(records={}, installed=[])

    with pytest.raises(LookupError):
        runtime.log_level.list_levels("GhostPlugin")
