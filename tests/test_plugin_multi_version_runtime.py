"""本体与分身同时加载同一插件不同版本源码的并存运行测试。

这是整个多版本能力的主链路，此前只有三段互不相连的覆盖：加载器隔离测试用的是
同一份源码、并存测试只验证目录解析、版本绑定测试的启动端口是桩。把三段接起来
真正执行一次，才能证明「两个实例跑着不同版本的代码」这件事成立——本体版本绑定
被忽略、实例命名空间串味、版本目录解析错位这三类缺陷都只会在这一层暴露。
"""

from __future__ import annotations

import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.binding import PluginVersionBinding
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.runtime import _stop_plugin_for_version_binding
from app.runtime.extensions.plugin.storage import (
    PluginInstanceDirectory,
    PluginInstanceStore,
    PluginStorage,
)
from app.runtime.extensions.plugin.version import (
    plugin_version_dir_name,
    register_plugin_version,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus


@pytest.fixture(autouse=True)
def purge_loaded_plugin_modules():
    """每个用例前后清掉本文件加载过的插件模块，避免用例之间互相顶替。

    本体的加载路径会读 ``sys.modules`` 缓存（生产里由 ``lifecycle.stop`` 的模块
    清理兜住），不清的话前一个用例留下的模块会被后一个用例直接复用，断言看起来
    通过、实际上根本没走加载。
    """

    def purge() -> None:
        """移除 app.plugins 下本文件用到的模块条目。"""
        for name in [
            name
            for name in sys.modules
            if name == "app.plugins.demoplugin" or name.startswith("app.plugins.demoplugin")
        ]:
            del sys.modules[name]

    purge()
    yield
    purge()


def _logger() -> SimpleNamespace:
    """提供加载器所需的最小日志端口。"""
    return SimpleNamespace(
        debug=lambda *_args: None,
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
    )


def _write_version(plugins_root: Path, plugin_id: str, version: str, marker: str) -> None:
    """落地一个可加载的版本目录，并把版本登记进已装清单。

    :param plugins_root: 插件根目录的父目录
    :param plugin_id: 插件目录名，须为小写
    :param version: 版本号
    :param marker: 该版本源码里可观测的返回值，用来区分实际执行的是哪一份代码
    """
    plugin_root = plugins_root / plugin_id
    version_dir = plugin_root / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(
        "class DemoPlugin:\n"
        f"    plugin_version = {version!r}\n"
        "    plugin_name = 'Demo'\n"
        "    plugin_config_prefix = 'demo_'\n"
        "    def init_plugin(self, _config=None):\n"
        "        pass\n"
        "    def marker(self):\n"
        f"        return {marker!r}\n",
        encoding="utf-8",
    )
    register_plugin_version(plugin_root, version, source="test")


def _write_lifecycle_version(
    plugins_root: Path,
    plugin_id: str,
    version: str,
    marker: str,
    *,
    fail_init: bool = False,
) -> None:
    """落地包含真实生命周期契约的版本源码。"""
    plugin_root = plugins_root / plugin_id
    version_dir = plugin_root / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(
        "class DemoPlugin:\n"
        f"    plugin_version = {version!r}\n"
        "    plugin_name = 'Demo'\n"
        "    plugin_config_prefix = 'demo_'\n"
        "    def init_plugin(self, _config=None):\n"
        f"        {'raise RuntimeError(\"broken version\")' if fail_init else 'pass'}\n"
        "    def get_state(self):\n"
        "        return True\n"
        f"    def marker(self):\n        return {marker!r}\n",
        encoding="utf-8",
    )
    register_plugin_version(plugin_root, version, source="test")


def _loader(plugins_root: Path, host_binding: PluginInstance | None) -> PluginLoader:
    """构造挂接指定本体版本绑定的加载器。"""
    return PluginLoader(
        plugins_root=plugins_root,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
        host_binding=lambda _plugin_id: host_binding,
    )


def _validator(candidate: object) -> bool:
    """按宿主插件的最小契约筛选候选类。"""
    return hasattr(candidate, "init_plugin")


def test_host_and_clone_execute_different_versions_at_the_same_time(tmp_path: Path):
    """本体钉在旧版本、分身跟随当前版本时，两者同时执行各自版本的代码。

    本体的源码目录只由它的版本绑定记录解析，绑定没被落盘或没被读取时，本体会
    静默加载当前版本，两个实例跑的就是同一份代码——从外部看仍然「启动成功」，
    只有在这里比对实际执行结果才发现。
    """
    _write_version(tmp_path, "demoplugin", "1.0.0", "v1")
    _write_version(tmp_path, "demoplugin", "2.0.0", "v2")
    host_binding = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="1.0.0",
    )
    loader = _loader(tmp_path, host_binding)

    host_class = loader.load("DemoPlugin", ["DemoPlugin"], _validator)[0]
    clone_class = loader.load_instance(
        PluginInstance(
            instance_id="DemoPluginWork",
            source_plugin_id="DemoPlugin",
        ),
        _validator,
    )[0]

    assert host_class.plugin_version == "1.0.0"
    assert clone_class.plugin_version == "2.0.0"
    assert host_class().marker() == "v1"
    assert clone_class().marker() == "v2"
    assert host_class is not clone_class


def test_pinning_the_host_version_actually_loads_that_version(tmp_path: Path):
    """给本体钉版本后，真正被加载执行的就是被钉的那个版本。

    这里把版本绑定与加载器接在一起跑完整条链路，是本体钉版失效那条缺陷唯一能
    暴露的位置：绑定若只写跟随开关、不落盘目标版本，本体加载会按记录里的旧值
    解析目录、静默回落到当前版本，而切换接口仍然回报成功。断言停在「start 收到
    了哪个版本号」这一层是看不出来的——那正是原有测试的盲区。
    """
    _write_version(tmp_path, "demoplugin", "1.0.0", "v1")
    _write_version(tmp_path, "demoplugin", "2.0.0", "v2")

    # 走真实的实例存储：本体记录以插件规范 ID 落盘，而加载器只掌握小写目录名，
    # 两者的键差异只有在真实存储语义下才会暴露。
    records: dict[str, PluginInstance] = {}
    directory = PluginInstanceDirectory(
        get=records.get,
        list_all=lambda: list(records.values()),
        list_by_source=lambda source_plugin_id: [
            record for record in records.values()
            if record.source_plugin_id == source_plugin_id
        ],
        save=lambda instance: records.__setitem__(instance.instance_id, instance),
        delete=lambda instance_id: records.pop(instance_id, None) is not None,
    )
    store = PluginInstanceStore(
        storage=lambda: PluginStorage(
            read=lambda _key: None,
            write=lambda _key, _value: None,
        ),
        directory=lambda: directory,
    )
    loaded: list[object] = []
    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
        # 与生产同款接线：加载器拿磁盘目录名去查，而记录以规范 ID 落盘
        host_binding=store.get_host,
    )

    def start(instance_id: str, _version: str | None) -> dict[str, PluginRuntimeStatus]:
        """按当前落盘的绑定真实加载一次插件。"""
        loaded.clear()
        loaded.extend(loader.load(instance_id, [instance_id], _validator))
        return {instance_id: PluginRuntimeStatus.ACTIVE}

    binding = PluginVersionBinding(
        plugins_root=tmp_path,
        plugin_exists=lambda _plugin_id: True,
        get_instance=lambda _instance_id: None,
        instances_for_source=lambda _source_plugin_id: [],
        all_instances_for_source=lambda _source_plugin_id: [],
        save_instance=lambda _instance: None,
        get_host_instance=store.get_host,
        save_host_instance=store.save_host,
        running=dict,
        start=start,
        stop=lambda _instance_id: None,
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        log=_logger(),
    )

    success, _message = binding.set_instance_version(
        "DemoPlugin", pinned_version="1.0.0"
    )

    assert success is True
    assert loaded, "切换后应当真实加载出插件类"
    assert loaded[0].plugin_version == "1.0.0"
    assert loaded[0]().marker() == "v1"


def test_two_clones_execute_different_pinned_versions_at_the_same_time(tmp_path: Path):
    """两个分身各自钉住不同版本时，同时执行各自版本的代码。

    分身的源码目录由 ``load_instance`` 的显式版本参数解析，两个实例共享同一份
    磁盘源码却必须落到不同的版本目录，且各自的模块命名空间互不覆盖。
    """
    _write_version(tmp_path, "demoplugin", "1.0.0", "v1")
    _write_version(tmp_path, "demoplugin", "2.0.0", "v2")
    loader = _loader(tmp_path, None)

    old_class = loader.load_instance(
        PluginInstance(
            instance_id="DemoPluginOld",
            source_plugin_id="DemoPlugin",
            pinned_version="1.0.0",
        ),
        _validator,
        version="1.0.0",
    )[0]
    new_class = loader.load_instance(
        PluginInstance(
            instance_id="DemoPluginNew",
            source_plugin_id="DemoPlugin",
            pinned_version="2.0.0",
        ),
        _validator,
        version="2.0.0",
    )[0]

    assert old_class().marker() == "v1"
    assert new_class().marker() == "v2"
    assert old_class.__name__ == "DemoPluginOld"
    assert new_class.__name__ == "DemoPluginNew"
    assert old_class.__module__ != new_class.__module__
    assert old_class.plugin_source_id == new_class.plugin_source_id == "DemoPlugin"


def _build_binding_lifecycle_e2e(
    tmp_path: Path,
    *,
    fail_new_version: bool = False,
) -> tuple[
    dict[str, PluginInstance],
    dict[str, object],
    PluginLifecycle,
    PluginVersionBinding,
    list[str],
]:
    """构造真实源码、加载器、生命周期和版本绑定组成的测试运行图。"""
    _write_lifecycle_version(tmp_path, "demoplugin", "1.0.0", "v1")
    _write_lifecycle_version(
        tmp_path,
        "demoplugin",
        "2.0.0",
        "v2",
        fail_init=fail_new_version,
    )
    records = {
        "DemoPlugin": PluginInstance(
            instance_id="DemoPlugin",
            source_plugin_id="DemoPlugin",
            mode="host",
            pinned_version="1.0.0",
        ),
        "DemoPluginWork": PluginInstance(
            instance_id="DemoPluginWork",
            source_plugin_id="DemoPlugin",
            mode="virtual",
            pinned_version="1.0.0",
        ),
    }
    running: dict[str, object] = {}
    classes: dict[str, object] = {}
    stop_calls: list[str] = []

    def get_host(plugin_id: str) -> PluginInstance | None:
        """按插件规范 ID 读取本体绑定。"""
        return next(
            (
                record
                for instance_id, record in records.items()
                if record.mode == "host" and instance_id.casefold() == plugin_id.casefold()
            ),
            None,
        )

    def load_plugins(plugin_id, installed, validator, version=None):
        """把真实 Loader 接入 Lifecycle 的宿主与分身两条加载分支。"""
        if plugin_id.casefold() == "demoplugin":
            return loader.load(plugin_id, installed, validator)
        instance = records[plugin_id]
        return loader.load_instance(instance, validator, version=version)

    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
        host_binding=get_host,
    )
    lifecycle = PluginLifecycle(
        classes=classes,
        running=running,
        load_plugins=load_plugins,
        loadable_plugins=lambda: ["DemoPlugin"],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: True,
        clear_modules=loader.clear_modules,
        clear_tools=lambda: None,
        enable_events=lambda _plugin: None,
        disable_events=lambda _plugin: None,
        runtime_status_writer=lambda _plugin_id, _status: None,
        database=lambda: PluginDatabase(),
        log=_logger(),
        event_sender=lambda *_args, **_kwargs: None,
    )
    binding = PluginVersionBinding(
        plugins_root=tmp_path,
        plugin_exists=lambda plugin_id: plugin_id.casefold() == "demoplugin",
        get_instance=lambda instance_id: records.get(instance_id),
        instances_for_source=lambda source_plugin_id: [
            record
            for record in records.values()
            if not record.is_host
            and record.source_plugin_id.casefold() == source_plugin_id.casefold()
        ],
        all_instances_for_source=lambda source_plugin_id: [
            record
            for record in records.values()
            if not record.is_host
            and record.source_plugin_id.casefold() == source_plugin_id.casefold()
        ],
        save_instance=lambda instance: records.__setitem__(instance.instance_id, instance),
        get_host_instance=get_host,
        save_host_instance=lambda instance: records.__setitem__(instance.instance_id, instance),
        running=lambda: running,
        start=lambda instance_id, version: lifecycle.start(instance_id, version=version),
        stop=lambda instance_id: _stop_plugin_for_version_binding(lifecycle, instance_id),
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        log=_logger(),
    )

    return records, running, lifecycle, binding, stop_calls


def test_binding_lifecycle_loader_e2e_keeps_host_and_clone_versions_isolated(
    tmp_path: Path,
):
    """绑定切换接入真实 Loader/Lifecycle 后，宿主与分身仍执行各自版本并收敛旧实例。"""
    records, running, _lifecycle, binding, stop_calls = _build_binding_lifecycle_e2e(
        tmp_path
    )
    assert _lifecycle.start("DemoPlugin") == {"DemoPlugin": PluginRuntimeStatus.ACTIVE}
    assert _lifecycle.start("DemoPluginWork") == {"DemoPluginWork": PluginRuntimeStatus.ACTIVE}
    running["DemoPlugin"].stop_service = lambda: stop_calls.append("DemoPlugin")
    running["DemoPluginWork"].stop_service = lambda: stop_calls.append("DemoPluginWork")
    # 通过生产管理器的同一把切换锁并发切换本体与分身；若锁只包住持久化而不包住
    # 生命周期，两个 start 会互相覆盖运行表，至少有一个旧实例的 stop_service
    # 会被漏掉。
    manager = PluginManager.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_version_binding = binding
    manager.mutation = lambda _operation: nullcontext()
    results: dict[str, tuple[bool, str]] = {}

    def switch(instance_id: str) -> None:
        """在工作线程中执行一个完整的 manager 版本切换。"""
        results[instance_id] = manager.set_plugin_instance_version(
            instance_id,
            pinned_version="2.0.0",
        )

    threads = [
        threading.Thread(target=switch, args=(instance_id,))
        for instance_id in ("DemoPlugin", "DemoPluginWork")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {
        "DemoPlugin": (True, "DemoPlugin"),
        "DemoPluginWork": (True, "DemoPluginWork"),
    }
    assert running["DemoPluginWork"].marker() == "v2"
    assert running["DemoPlugin"].marker() == "v2"
    assert running["DemoPlugin"].plugin_version == "2.0.0"
    assert running["DemoPluginWork"].plugin_version == "2.0.0"
    assert sorted(stop_calls) == ["DemoPlugin", "DemoPluginWork"]


def test_binding_stop_service_failure_keeps_old_runtime_and_skips_target_start(
    tmp_path: Path,
):
    """真实 stop_service 未收敛时，版本切换不得覆盖仍在运行的旧实例。"""
    records, running, lifecycle, binding, _stop_calls = _build_binding_lifecycle_e2e(
        tmp_path
    )
    assert lifecycle.start("DemoPlugin") == {"DemoPlugin": PluginRuntimeStatus.ACTIVE}
    assert lifecycle.start("DemoPluginWork") == {"DemoPluginWork": PluginRuntimeStatus.ACTIVE}
    old_runtime = running["DemoPluginWork"]

    def fail_stop_service() -> None:
        """模拟插件后台服务拒绝停止。"""
        raise RuntimeError("stop failed")

    old_runtime.stop_service = fail_stop_service

    success, message = binding.set_instance_version(
        "DemoPluginWork",
        pinned_version="2.0.0",
    )

    assert success is False
    assert "停止旧实例失败" in message
    assert running["DemoPluginWork"] is old_runtime
    assert running["DemoPluginWork"].plugin_version == "1.0.0"
    assert records["DemoPluginWork"].pinned_version == "1.0.0"


@pytest.mark.parametrize("instance_id", ["DemoPlugin", "DemoPluginWork"])
@pytest.mark.parametrize("requested_version", [None, "2.0.0"])
def test_binding_lifecycle_loader_e2e_falls_back_from_broken_version(
    tmp_path: Path,
    instance_id: str,
    requested_version: str | None,
):
    """真实失败源码下，宿主和分身都按切换前实际运行版本完成回退。"""
    records, running, lifecycle, binding, stop_calls = _build_binding_lifecycle_e2e(
        tmp_path,
        fail_new_version=True,
    )
    # 先按旧钉定版本启动，再把绑定改成跟随当前，模拟当前清单已指向 2.0.0、
    # 实际仍运行 1.0.0 的进程状态。回退必须读取 running 对象的版本，而不是依赖
    # 可能已被切换请求覆盖的持久化 plugin_version。
    assert lifecycle.start("DemoPlugin", version="1.0.0") == {
        "DemoPlugin": PluginRuntimeStatus.ACTIVE
    }
    assert lifecycle.start("DemoPluginWork", version="1.0.0") == {
        "DemoPluginWork": PluginRuntimeStatus.ACTIVE
    }
    for record_id, record in tuple(records.items()):
        records[record_id] = record.model_copy(
            update={"pinned_version": None}
        )
    running["DemoPlugin"].stop_service = lambda: stop_calls.append("DemoPlugin")
    running["DemoPluginWork"].stop_service = lambda: stop_calls.append("DemoPluginWork")

    success, message = binding.set_instance_version(
        instance_id,
        pinned_version=requested_version,
    )

    assert success is False
    assert "已回退到原版本 1.0.0" in message
    assert running[instance_id].plugin_version == "1.0.0"
    assert running[instance_id].marker() == "v1"
    assert records[instance_id].pinned_version is None
    assert stop_calls == [instance_id]
