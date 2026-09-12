"""插件已装版本总览与实例版本绑定切换测试。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin.binding import PluginVersionBinding
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.extensions.plugin.version import (
    plugin_version_dir_name,
    read_plugin_versions_manifest,
    write_plugin_versions_manifest,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus


def _logger() -> SimpleNamespace:
    """提供绑定服务测试所需的最小日志端口。"""
    return SimpleNamespace(
        debug=lambda *_args: None,
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
    )


def _write_version_dir(plugins_root: Path, plugin_id: str, version: str) -> Path:
    """在插件根目录下创建一个空的版本目录。"""
    version_dir = plugins_root / plugin_id / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True)
    return version_dir


def _write_manifest(
    plugin_root: Path, entries: list[tuple[str, str]], current: str | None
) -> None:
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


def _stamp_installed_at(plugin_root: Path, stamps: dict[str, str]) -> None:
    """把已装版本清单里各版本的登记时间改写为指定值，消除真实时钟带来的顺序不确定性。"""
    manifest = read_plugin_versions_manifest(plugin_root)
    for entry in manifest["versions"]:
        if entry["version"] in stamps:
            entry["installed_at"] = stamps[entry["version"]]
    write_plugin_versions_manifest(plugin_root, manifest["versions"], manifest["current"])


class _Harness:
    """组装 PluginVersionBinding 依赖并记录调用轨迹的测试脚手架。"""

    def __init__(
        self,
        *,
        plugins_root: Path,
        instances: dict[str, PluginInstance] | None = None,
        host_instances: dict[str, PluginInstance] | None = None,
        plugin_exists: bool = True,
        known_plugin_ids: set[str] | None = None,
        start_results: dict | None = None,
        multi_version_blockers: list[str] | None = None,
        running_ids: set[str] | None = None,
        running_versions: dict[str, str] | None = None,
        refresh_registrations=None,
        pending_installation=None,
        instances_for_source_error: Exception | None = None,
        display_names: dict[str, str] | None = None,
    ) -> None:
        self.plugins_root = plugins_root
        self.instances: dict[str, PluginInstance] = dict(instances or {})
        self.host_instances: dict[str, PluginInstance] = dict(host_instances or {})
        self.saved: list[PluginInstance] = []
        self.saved_hosts: list[PluginInstance] = []
        self.stopped: list[str] = []
        self.start_calls: list[tuple[str, str | None]] = []
        self._start_results = start_results or {}
        self._plugin_exists_flag = plugin_exists
        # None 保持旧语义：不论查询哪个 ID 都直接返回 plugin_exists 这一个布尔值；
        # 只有显式传入 known_plugin_ids 时才按 ID 精确判定，供需要区分「已知插件」
        # 与「任意未知 ID」的用例使用（例如本体解析回退路径）。
        self._known_plugin_ids = known_plugin_ids
        self._multi_version_blockers_result = (
            [] if multi_version_blockers is None else multi_version_blockers
        )
        self.multi_version_blockers_calls: list[tuple[str, list[Path]]] = []
        self._running_ids = running_ids or set()
        self._running_versions = running_versions or {}
        self._instances_for_source_error = instances_for_source_error
        self._display_names = display_names or {}
        self.logger = _logger()
        self.service = PluginVersionBinding(
            plugins_root=plugins_root,
            plugin_exists=self._plugin_exists,
            get_instance=self.instances.get,
            instances_for_source=self._instances_for_source,
            all_instances_for_source=self._instances_for_source,
            save_instance=self._save_instance,
            get_host_instance=self.host_instances.get,
            save_host_instance=self._save_host_instance,
            running=lambda: {
                plugin_id: SimpleNamespace(
                    plugin_version=self._running_versions.get(plugin_id)
                )
                for plugin_id in self._running_ids
            },
            start=self._start,
            stop=self.stopped.append,
            multi_version_blockers=self._multi_version_blockers,
            display_name=self._display_names.get,
            log=self.logger,
            refresh_registrations=refresh_registrations,
            pending_installation=pending_installation,
        )

    def _plugin_exists(self, plugin_id: str) -> bool:
        if self._known_plugin_ids is not None:
            return self._plugin_exists_flag and plugin_id in self._known_plugin_ids
        return self._plugin_exists_flag

    def _instances_for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        if self._instances_for_source_error is not None:
            raise self._instances_for_source_error
        return [
            instance
            for instance in self.instances.values()
            if instance.source_plugin_id == source_plugin_id
        ]

    def _save_instance(self, instance: PluginInstance) -> None:
        self.instances[instance.instance_id] = instance
        self.saved.append(instance)

    def _save_host_instance(self, instance: PluginInstance) -> None:
        self.host_instances[instance.instance_id] = instance
        self.saved_hosts.append(instance)

    def _start(self, instance_id: str, version: str | None) -> dict:
        self.start_calls.append((instance_id, version))
        status = self._start_results.get(version, PluginRuntimeStatus.ACTIVE)
        return {instance_id: status}

    def _multi_version_blockers(self, plugin_id: str, source_dirs: list[Path]) -> list[str]:
        self.multi_version_blockers_calls.append((plugin_id, list(source_dirs)))
        return self._multi_version_blockers_result


# 一、已装版本总览


def test_overview_lists_installed_versions_and_instance_bindings(tmp_path: Path):
    """总览含已装版本落盘信息与各实例的版本绑定和运行状态。"""
    plugin_root = tmp_path / "demoplugin"
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_manifest(
        plugin_root, [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="2.0.0"
    )
    work = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_name="工作实例",
        pinned_version="1.0.0",
    )
    home = PluginInstance(instance_id="DemoPluginHome", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": work, "DemoPluginHome": home},
        running_ids={"DemoPluginWork"},
        running_versions={"DemoPluginWork": "1.0.0"},
        display_names={"DemoPlugin": "演示插件"},
    )

    overview = harness.service.overview("DemoPlugin")

    assert overview["plugin_id"] == "DemoPlugin"
    assert overview["current_version"] == "2.0.0"
    assert [item["version"] for item in overview["installed_versions"]] == [
        "1.0.0",
        "2.0.0",
    ]
    assert overview["installed_versions"][1]["is_current"] is True
    assert overview["installed_versions"][0]["is_current"] is False
    bindings = {item["instance_id"]: item for item in overview["instances"]}
    assert bindings["DemoPluginWork"] == {
        "instance_id": "DemoPluginWork",
        "plugin_name": "工作实例",
        "pinned_version": "1.0.0",
        "running": True,
        "running_version": "1.0.0",
        "is_host": False,
        "is_default_target": False,
            "is_enabled": True,
    }
    assert bindings["DemoPluginHome"]["running"] is False
    assert bindings["DemoPluginHome"]["running_version"] is None
    assert bindings["DemoPluginHome"]["pinned_version"] is None
    assert bindings["DemoPluginHome"]["is_host"] is False
    assert bindings["DemoPluginHome"]["is_default_target"] is False
    assert bindings["DemoPlugin"] == {
        "instance_id": "DemoPlugin",
        "plugin_name": "演示插件",
        "pinned_version": None,
        "running": False,
        "running_version": None,
        "is_host": True,
        "is_default_target": False,
            "is_enabled": True,
    }
    assert overview["instances"][0]["is_host"] is True


def test_overview_raises_lookup_error_for_unknown_plugin(tmp_path: Path):
    """插件不存在时抛出 LookupError，不返回空壳总览掩盖问题。"""
    harness = _Harness(plugins_root=tmp_path, plugin_exists=False)

    with pytest.raises(LookupError):
        harness.service.overview("Missing")


def test_overview_rejects_clone_own_id_as_plugin_id(tmp_path: Path):
    """用分身自身的实例 ID 查询总览必须拒绝，不能把分身伪装成本体。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": clone},
    )

    with pytest.raises(LookupError):
        harness.service.overview("DemoPluginWork")


# 二、实例版本绑定切换


def test_set_instance_version_switches_to_pinned_version(tmp_path: Path):
    """唯一实例切到已安装的目标版本时直接成功，不触发并存检查。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is True
    assert message == "DemoPluginWork"
    assert harness.instances["DemoPluginWork"].pinned_version is not None
    assert harness.stopped == ["DemoPluginWork"]
    assert harness.start_calls == [("DemoPluginWork", "2.0.0")]
    assert harness.multi_version_blockers_calls == []


def test_set_instance_version_refreshes_host_registrations_after_restart(tmp_path: Path):
    """版本切换成功后刷新该实例的 API、调度和命令注册投影。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    refreshed: list[str] = []
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        refresh_registrations=refreshed.append,
    )

    success, _message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is True
    assert refreshed == ["DemoPluginWork"]


def test_set_instance_version_refreshes_fastapi_routes_using_actual_new_version(
    tmp_path: Path,
):
    """真实动态路由刷新回调必须读取切换后的运行实例版本。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    records = {instance.instance_id: instance}
    running = {instance.instance_id: SimpleNamespace(plugin_version="1.0.0")}
    app = FastAPI()

    def plugin_apis(plugin_id: str) -> list[dict]:
        """把实际运行版本投影到动态路由路径，模拟旧 callback 的读取方式。"""
        return [{
            "path": f"/{plugin_id}/{running[plugin_id].plugin_version}",
            "endpoint": lambda: {"ok": True},
            "methods": ["GET"],
            "allow_anonymous": True,
        }]

    registry = FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=lambda: list(running),
        plugin_apis=plugin_apis,
        verify_token=lambda: None,
        verify_apikey=lambda: None,
        prefix="/api/v1/plugin",
        protected_routes=set(),
        log=_logger(),
    )
    registry.update("DemoPluginWork", "add")

    def start(instance_id: str, version: str | None) -> dict:
        """把生命周期启动结果写进真实运行表。"""
        running[instance_id] = SimpleNamespace(plugin_version=version)
        return {instance_id: PluginRuntimeStatus.ACTIVE}

    def stop(instance_id: str) -> None:
        """从真实运行表移除旧实例。"""
        running.pop(instance_id, None)

    binding = PluginVersionBinding(
        plugins_root=tmp_path,
        plugin_exists=lambda _plugin_id: True,
        get_instance=records.get,
        instances_for_source=lambda _source_plugin_id: [],
        all_instances_for_source=lambda _source_plugin_id: [],
        save_instance=lambda updated: records.__setitem__(updated.instance_id, updated),
        get_host_instance=lambda _plugin_id: None,
        save_host_instance=lambda _updated: None,
        running=lambda: running,
        start=start,
        stop=stop,
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        refresh_registrations=lambda plugin_id: registry.update(plugin_id, "add"),
        log=_logger(),
    )

    success, _message = binding.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is True
    route_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/plugin/DemoPluginWork/")
    }
    assert route_paths == {"/api/v1/plugin/DemoPluginWork/2.0.0"}


def test_set_instance_version_rejects_uninstalled_target(tmp_path: Path):
    """目标版本未安装时拒绝切换，不停止也不启动实例。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(plugins_root=tmp_path, instances={"DemoPluginWork": instance})

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="9.9.9"
    )

    assert success is False
    assert "未安装版本 9.9.9" in message
    assert harness.stopped == []
    assert harness.start_calls == []


def test_set_instance_version_does_not_start_when_stop_reports_failure(tmp_path: Path):
    """旧实例明确报告停止失败时不得继续启动目标版本覆盖它。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(plugins_root=tmp_path, instances={"DemoPluginWork": instance})
    harness.service._stop = lambda _instance_id: False

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "停止旧实例失败" in message
    assert harness.start_calls == []
    assert harness.instances["DemoPluginWork"].pinned_version == "1.0.0"


def test_set_instance_version_rejects_active_package_write(tmp_path: Path):
    """插件包写入窗口内拒绝切版，避免和安装回载及路由刷新交叉。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={instance.instance_id: instance},
        pending_installation=lambda _plugin_id: True,
    )

    success, message = harness.service.set_instance_version(
        instance.instance_id,
        pinned_version="2.0.0",
    )

    assert success is False
    assert "正在安装或写入" in message
    assert harness.stopped == []
    assert harness.start_calls == []
    assert harness.instances[instance.instance_id].pinned_version == "1.0.0"


def test_manager_serializes_same_instance_switches_and_stops_first_new_runtime():
    """同一实例并发切版必须串行，第二次切换要停止第一次已启动的实例。"""
    manager = PluginManager.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager.mutation = lambda _operation: nullcontext()
    first_started = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    running = {"DemoPluginWork": SimpleNamespace(plugin_version="1.0.0")}
    stop_calls: list[str] = []
    versions: list[str] = []

    class Binding:
        """按可控事件模拟实际绑定服务的生命周期边界。"""

        def set_instance_version(
            self,
            instance_id: str,
            *,
            pinned_version: str | None = None,
        ) -> tuple[bool, str]:
            assert pinned_version is not None
            versions.append(pinned_version)
            if pinned_version == "2.0.0":
                first = SimpleNamespace(
                    pinned_version="2.0.0",
                    stop_service=lambda: stop_calls.append("2.0.0"),
                )
                first_started.set()
                assert release_first.wait(timeout=2)
                running[instance_id] = first
            else:
                second_entered.set()
                running[instance_id].stop_service()
                running[instance_id] = SimpleNamespace(plugin_version="3.0.0")
            return True, instance_id

    manager._plugin_version_binding = Binding()
    results: dict[str, tuple[bool, str]] = {}
    errors: list[BaseException] = []

    def switch(version: str) -> None:
        """在线程中执行一次同实例切版，并保留线程异常。"""
        try:
            results[version] = manager.set_plugin_instance_version(
                "DemoPluginWork",
                pinned_version=version,
            )
        except BaseException as error:  # noqa: BLE001 - 测试线程异常需显式失败
            errors.append(error)

    first_thread = threading.Thread(target=switch, args=("2.0.0",))
    second_thread = threading.Thread(target=switch, args=("3.0.0",))
    first_thread.start()
    assert first_started.wait(timeout=2)
    second_thread.start()
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert versions == ["2.0.0", "3.0.0"]
    assert results == {
        "2.0.0": (True, "DemoPluginWork"),
        "3.0.0": (True, "DemoPluginWork"),
    }
    assert stop_calls == ["2.0.0"]
    assert running["DemoPluginWork"].plugin_version == "3.0.0"


def test_manager_rejects_version_switch_during_package_write():
    """切版遇到包事务持锁时立即返回，不能与异步安装交叉。"""
    manager = PluginManager.__new__(PluginManager)
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_package_write_lock = threading.RLock()
    manager.mutation = lambda _operation: nullcontext()
    called: list[str] = []

    class Binding:
        """记录不应发生的绑定切换调用。"""

        def set_instance_version(self, instance_id: str, **_kwargs) -> tuple[bool, str]:
            called.append(instance_id)
            return True, instance_id

    manager._plugin_version_binding = Binding()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_package_lock() -> None:
        """模拟安装事务跨越异步阶段持有包写入锁。"""
        manager._plugin_package_write_lock.acquire()
        lock_held.set()
        release_lock.wait(timeout=2)
        manager._plugin_package_write_lock.release()

    holder = threading.Thread(target=hold_package_lock)
    holder.start()
    assert lock_held.wait(timeout=2)
    try:
        result = manager.set_plugin_instance_version(
            "DemoPluginWork",
            pinned_version="2.0.0",
        )
    finally:
        release_lock.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert result[0] is False
    assert "插件包更新" in result[1]
    assert called == []


def test_set_instance_version_returns_failure_for_unknown_instance(tmp_path: Path):
    """实例不存在、且该 ID 也不是已知插件本体时直接返回失败，不产生任何副作用。"""
    harness = _Harness(plugins_root=tmp_path, known_plugin_ids=set())

    success, message = harness.service.set_instance_version(
        "Missing", pinned_version=None
    )

    assert success is False
    assert "不存在" in message
    assert harness.saved == []


def test_set_instance_version_rejects_when_would_create_unsupported_coexistence(
    tmp_path: Path,
):
    """切换会让插件多版本并存且写法不支持时拒绝，且不改动任何状态。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_manifest(
        tmp_path / "demoplugin", [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="1.0.0"
    )
    target = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    sibling = PluginInstance(instance_id="DemoPluginHome", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": target, "DemoPluginHome": sibling},
        multi_version_blockers=["存在自引用绝对导入"],
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "多版本并存" in message
    assert harness.saved == []
    assert harness.stopped == []
    assert harness.start_calls == []
    assert harness.multi_version_blockers_calls[0][0] == "demoplugin"


def test_set_instance_version_allows_coexistence_when_no_blockers_found(tmp_path: Path):
    """并存会发生但静态扫描未命中阻断时允许切换。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_manifest(
        tmp_path / "demoplugin", [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="1.0.0"
    )
    target = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    sibling = PluginInstance(instance_id="DemoPluginHome", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": target, "DemoPluginHome": sibling},
        multi_version_blockers=[],
    )

    success, _message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is True
    assert harness.multi_version_blockers_calls != []


def test_set_instance_version_falls_back_to_previous_effective_version(tmp_path: Path):
    """目标版本启动失败时保持已生效版本不动，并以该版本重新启动完成回退。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        running_ids={"DemoPluginWork"},
        running_versions={"DemoPluginWork": "1.0.0"},
        start_results={"2.0.0": PluginRuntimeStatus.LOAD_FAILED, "1.0.0": PluginRuntimeStatus.ACTIVE},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "已回退到原版本 1.0.0" in message
    assert harness.start_calls == [
        ("DemoPluginWork", "2.0.0"),
        ("DemoPluginWork", "1.0.0"),
    ]


def test_set_instance_version_fails_without_retry_when_no_fallback_available(tmp_path: Path):
    """从未成功启动过时没有可回退的版本，启动失败即直接判定失败，不做二次尝试。"""
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        start_results={"2.0.0": PluginRuntimeStatus.LOAD_FAILED},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "没有可回退" not in message  # 面向用户的消息保持简洁
    assert harness.start_calls == [("DemoPluginWork", "2.0.0")]


def test_set_instance_version_reports_failure_when_fallback_also_fails(tmp_path: Path):
    """目标版本和回退版本均启动失败时，两次尝试都发生且给出明确失败信息。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        running_ids={"DemoPluginWork"},
        running_versions={"DemoPluginWork": "1.0.0"},
        start_results={
            "2.0.0": PluginRuntimeStatus.LOAD_FAILED,
            "1.0.0": PluginRuntimeStatus.LOAD_FAILED,
        },
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "同样失败" in message
    assert harness.start_calls == [
        ("DemoPluginWork", "2.0.0"),
        ("DemoPluginWork", "1.0.0"),
    ]


def test_failed_cleanup_refreshes_routes_when_binding_restore_also_fails(tmp_path: Path):
    """清理和绑定补偿都失败时仍按实际运行对象刷新旧注册。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    records = {instance.instance_id: instance}
    running = {instance.instance_id: SimpleNamespace(plugin_version="1.0.0")}
    stop_calls: list[str] = []
    refreshed: list[str] = []
    save_calls = 0

    def save(updated: PluginInstance) -> None:
        """让目标绑定写入成功、原绑定补偿失败。"""
        nonlocal save_calls
        save_calls += 1
        if save_calls > 1:
            raise RuntimeError("绑定存储不可用")
        records[updated.instance_id] = updated

    def stop(instance_id: str) -> bool:
        """首次停止旧版本成功，清理失败目标时模拟生命周期拒绝。"""
        stop_calls.append(instance_id)
        if len(stop_calls) == 1:
            running.pop(instance_id, None)
            return True
        raise RuntimeError("错误版本仍有资源")

    def start(instance_id: str, _version: str | None) -> dict[str, PluginRuntimeStatus]:
        """留下一个需要刷新注册投影的失败运行对象。"""
        running[instance_id] = SimpleNamespace(plugin_version="2.0.0")
        return {instance_id: PluginRuntimeStatus.LOAD_FAILED}

    binding = PluginVersionBinding(
        plugins_root=tmp_path,
        plugin_exists=lambda _plugin_id: True,
        get_instance=records.get,
        instances_for_source=lambda _source_plugin_id: [],
        all_instances_for_source=lambda _source_plugin_id: [],
        save_instance=save,
        get_host_instance=lambda _plugin_id: None,
        save_host_instance=lambda _instance: None,
        running=lambda: running,
        start=start,
        stop=stop,
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        refresh_registrations=refreshed.append,
        log=_logger(),
    )

    success, message = binding.set_instance_version(
        instance.instance_id,
        pinned_version="2.0.0",
    )

    assert success is False
    assert "清理错误运行实例失败" in message
    assert stop_calls == [instance.instance_id, instance.instance_id]
    assert refreshed == [instance.instance_id]


def test_failed_target_and_fallback_revoke_old_fastapi_route(tmp_path: Path):
    """目标和回退均失败后，旧动态路由不能继续调用已停止的实例。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    records = {instance.instance_id: instance}
    running = {instance.instance_id: SimpleNamespace(plugin_version="1.0.0")}
    app = FastAPI()

    def plugin_apis(plugin_id: str) -> list[dict]:
        """只为实际仍运行的实例投影动态路由。"""
        runtime = running.get(plugin_id)
        if runtime is None:
            return []
        return [{
            "path": f"/{plugin_id}/{runtime.plugin_version}",
            "endpoint": lambda: {"ok": True},
            "methods": ["GET"],
            "allow_anonymous": True,
        }]

    registry = FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=lambda: list(running),
        plugin_apis=plugin_apis,
        verify_token=lambda: None,
        verify_apikey=lambda: None,
        prefix="/api/v1/plugin",
        protected_routes=set(),
        log=_logger(),
    )
    registry.update(instance.instance_id, "add")

    def stop(instance_id: str) -> bool:
        """停止旧实例并让失败目标没有运行对象可供宿主继续注册。"""
        running.pop(instance_id, None)
        return True

    def start(instance_id: str, _version: str | None) -> dict:
        """模拟目标和回退版本都在初始化阶段失败。"""
        return {instance_id: PluginRuntimeStatus.LOAD_FAILED}

    binding = PluginVersionBinding(
        plugins_root=tmp_path,
        plugin_exists=lambda _plugin_id: True,
        get_instance=records.get,
        instances_for_source=lambda _source_plugin_id: [],
        all_instances_for_source=lambda _source_plugin_id: [],
        save_instance=lambda updated: records.__setitem__(updated.instance_id, updated),
        get_host_instance=lambda _plugin_id: None,
        save_host_instance=lambda _updated: None,
        running=lambda: running,
        start=start,
        stop=stop,
        multi_version_blockers=lambda _plugin_id, _dirs: [],
        refresh_registrations=lambda plugin_id: registry.update(plugin_id, "add"),
        log=_logger(),
    )

    success, message = binding.set_instance_version(
        instance.instance_id,
        pinned_version="2.0.0",
    )

    assert success is False
    assert "同样失败" in message
    assert running == {}
    route_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/plugin/DemoPluginWork/")
    }
    assert route_paths == set()

    async def request_old_route() -> httpx.Response:
        """通过真实 ASGI 路由表确认旧入口已经不可调用。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get("/api/v1/plugin/DemoPluginWork/1.0.0")

    response = asyncio.run(request_old_route())
    assert response.status_code == 404


def test_set_instance_version_switch_to_follow_current_does_not_retry(tmp_path: Path):
    """切回跟随当前版本失败时按单次尝试语义处理，不做回退重试，并复原原有绑定。

    失败后把记录留在「跟随当前版本」会让下一次加载继续用这个并未起来的版本，
    原先能跑的钉版就此丢失；因此单次尝试失败同样要把绑定复原成切换前的样子。
    """
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        start_results={None: PluginRuntimeStatus.LOAD_FAILED},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version=None
    )

    assert success is False
    assert "跟随当前版本失败" in message
    assert harness.start_calls == [("DemoPluginWork", None)]
    assert harness.instances["DemoPluginWork"].pinned_version is not None
    assert harness.instances["DemoPluginWork"].pinned_version == "1.0.0"


def test_set_instance_version_applies_to_source_plugin_host(tmp_path: Path):
    """instance_id 等于源插件 ID 且该插件存在时按本体解析，写回本体端口而非分身端口。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    harness = _Harness(plugins_root=tmp_path)

    success, message = harness.service.set_instance_version(
        "DemoPlugin", pinned_version="2.0.0"
    )

    assert success is True
    assert message == "DemoPlugin"
    assert harness.saved == []
    assert harness.saved_hosts[-1].instance_id == "DemoPlugin"
    assert harness.saved_hosts[-1].mode == "host"
    assert harness.host_instances["DemoPlugin"].pinned_version is not None
    assert harness.stopped == ["DemoPlugin"]
    assert harness.start_calls == [("DemoPlugin", "2.0.0")]


def test_set_instance_version_persists_pinned_version_for_host(tmp_path: Path):
    """本体钉版本必须把目标版本写进绑定记录。

    本体加载走的是 ``loader.load``，该路径不接收 start 的 version 形参，源码目录
    只由这条绑定记录解析；记录不带目标版本时切换会静默加载旧版本却回报成功。
    """
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    harness = _Harness(plugins_root=tmp_path)

    success, _message = harness.service.set_instance_version(
        "DemoPlugin", pinned_version="1.0.0"
    )

    assert success is True
    assert harness.host_instances["DemoPlugin"].pinned_version == "1.0.0"
    assert harness.host_instances["DemoPlugin"].pinned_version is not None


def test_set_instance_version_restores_binding_when_switch_fails(tmp_path: Path):
    """切换失败并以原版本回退成功时，绑定要复原成切换前的样子。

    落盘发生在停止再启动之前，失败后不复原会留下「声称钉在目标版本」却实际
    在跑另一个版本的记录。
    """
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    existing = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": existing},
        start_results={"2.0.0": PluginRuntimeStatus.LOAD_FAILED},
    )

    success, _message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert harness.instances["DemoPluginWork"].pinned_version == "1.0.0"
    assert harness.instances["DemoPluginWork"].pinned_version is not None


def test_set_instance_version_updates_existing_host_binding(tmp_path: Path):
    """本体已有版本绑定记录时，切换沿用同一条记录原地更新，不当作新分身处理。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    existing_host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="1.0.0",
    )
    harness = _Harness(plugins_root=tmp_path, host_instances={"DemoPlugin": existing_host})

    success, _message = harness.service.set_instance_version(
        "DemoPlugin", pinned_version=None
    )

    assert success is True
    assert harness.host_instances["DemoPlugin"].pinned_version is None
    assert harness.saved == []


def test_creates_version_coexistence_detects_divergence_from_pinned_host(tmp_path: Path):
    """本体被钉在某版本时，分身切到另一版本仍会被判定为制造多版本并存。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_manifest(
        tmp_path / "demoplugin", [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="1.0.0"
    )
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    pinned_host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": clone},
        host_instances={"DemoPlugin": pinned_host},
        multi_version_blockers=["存在自引用绝对导入"],
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="2.0.0"
    )

    assert success is False
    assert "多版本并存" in message
    assert harness.multi_version_blockers_calls != []


def test_creates_version_coexistence_uses_host_actual_pinned_version_not_manifest_current(
    tmp_path: Path,
):
    """并存判定按本体实际绑定版本核算，而不是无条件假设本体运行清单当前版本。

    清单当前版本是 2.0.0，但本体已被钉在 1.0.0；分身切到与本体实际绑定一致的
    1.0.0 时不应被判定为制造并存——旧实现无条件把清单当前版本当作本体的运行
    版本，会在这种场景下把这次完全安全的切换误判为并存并拒绝。
    """
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_manifest(
        tmp_path / "demoplugin", [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="2.0.0"
    )
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    pinned_host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": clone},
        host_instances={"DemoPlugin": pinned_host},
        multi_version_blockers=["存在自引用绝对导入"],
    )

    success, _message = harness.service.set_instance_version(
        "DemoPluginWork", pinned_version="1.0.0"
    )

    assert success is True
    assert harness.multi_version_blockers_calls == []


# 三、版本回收


def test_recycle_versions_raises_lookup_error_for_unknown_plugin(tmp_path: Path):
    """插件不存在时抛出 LookupError，不静默返回空回收结果。"""
    harness = _Harness(plugins_root=tmp_path, plugin_exists=False)

    with pytest.raises(LookupError):
        harness.service.recycle_versions("Missing")


def test_recycle_versions_protects_both_effective_and_expected_versions(tmp_path: Path):
    """引用集合并入已生效版本与按跟随开关解析出的期望版本，两者都受保护。

    四个已装版本按登记时间排列，默认保留窗口（2）只覆盖最近的 3.0.0 与
    4.0.0；1.0.0 既不是当前版本也不在保留窗口内，唯一能保住它的就是「被
    实例引用」这条判据——实例跟随当前版本（期望版本 3.0.0），但已生效版本
    仍是上次成功启动时的旧版本 1.0.0，二者必须都并入引用集合。
    """
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_version_dir(tmp_path, "demoplugin", "3.0.0")
    _write_version_dir(tmp_path, "demoplugin", "4.0.0")
    plugin_root = tmp_path / "demoplugin"
    _write_manifest(
        plugin_root,
        [
            ("1.0.0", "v1_0_0"),
            ("2.0.0", "v2_0_0"),
            ("3.0.0", "v3_0_0"),
            ("4.0.0", "v4_0_0"),
        ],
        current="3.0.0",
    )
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-02-01T00:00:00+00:00",
            "3.0.0": "2020-03-01T00:00:00+00:00",
            "4.0.0": "2020-04-01T00:00:00+00:00",
        },
    )
    following = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        pinned_version=None,
    )
    # 跟随当前版本的实例不留锚定值，它正在跑的版本由运行态提供并据此免于回收
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": following},
        running_ids={"DemoPluginWork"},
        running_versions={"DemoPluginWork": "1.0.0"},
    )

    outcome = harness.service.recycle_versions("DemoPlugin")

    assert outcome["removed"] == ["2.0.0"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()
    assert (plugin_root / "v4_0_0").is_dir()
    assert not (plugin_root / "v2_0_0").exists()


def test_recycle_versions_protects_hosts_effective_version_with_no_clones(tmp_path: Path):
    """引用集合同样纳入本体的已生效版本，即便该插件没有任何分身实例。

    四个已装版本，保留窗口只覆盖最近的 3.0.0 与 4.0.0；本体跟随当前版本（期望
    版本 3.0.0），但已生效版本仍是上次成功启动时的旧版本 1.0.0——遗漏本体会
    误删它正在用的版本。
    """
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    _write_version_dir(tmp_path, "demoplugin", "3.0.0")
    _write_version_dir(tmp_path, "demoplugin", "4.0.0")
    plugin_root = tmp_path / "demoplugin"
    _write_manifest(
        plugin_root,
        [
            ("1.0.0", "v1_0_0"),
            ("2.0.0", "v2_0_0"),
            ("3.0.0", "v3_0_0"),
            ("4.0.0", "v4_0_0"),
        ],
        current="3.0.0",
    )
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-02-01T00:00:00+00:00",
            "3.0.0": "2020-03-01T00:00:00+00:00",
            "4.0.0": "2020-04-01T00:00:00+00:00",
        },
    )
    host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version=None,
    )
    # 本体同理：跟随时靠运行态版本免于回收，而不是靠一份持久化副本
    harness = _Harness(
        plugins_root=tmp_path,
        host_instances={"DemoPlugin": host},
        running_ids={"DemoPlugin"},
        running_versions={"DemoPlugin": "1.0.0"},
    )

    outcome = harness.service.recycle_versions("DemoPlugin")

    assert outcome["removed"] == ["2.0.0"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()
    assert (plugin_root / "v4_0_0").is_dir()
    assert not (plugin_root / "v2_0_0").exists()


def test_recycle_versions_protects_actual_running_version_outside_retention(
    tmp_path: Path,
):
    """实际运行版本与绑定记录不一致时，回收仍须保留运行中的旧目录。"""
    for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
        _write_version_dir(tmp_path, "demoplugin", version)
    plugin_root = tmp_path / "demoplugin"
    _write_manifest(
        plugin_root,
        [
            ("1.0.0", "v1_0_0"),
            ("2.0.0", "v2_0_0"),
            ("3.0.0", "v3_0_0"),
            ("4.0.0", "v4_0_0"),
        ],
        current="3.0.0",
    )
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-02-01T00:00:00+00:00",
            "3.0.0": "2020-03-01T00:00:00+00:00",
            "4.0.0": "2020-04-01T00:00:00+00:00",
        },
    )
    host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="2.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        host_instances={"DemoPlugin": host},
        running_ids={"DemoPlugin"},
        running_versions={"DemoPlugin": "1.0.0"},
    )

    outcome = harness.service.recycle_versions("DemoPlugin")

    assert outcome["removed"] == []
    assert outcome["kept"]["1.0.0"] == (
        "被实例引用（已生效版本或按跟随开关解析出的期望版本）"
    )
    assert (plugin_root / "v1_0_0").is_dir()


def test_recycle_versions_skips_when_install_journal_is_pending(tmp_path: Path):
    """安装 journal 未收尾时不删除任何版本，保持 COMMITTED receipt 可核验。"""
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        _write_version_dir(tmp_path, "demoplugin", version)
    plugin_root = tmp_path / "demoplugin"
    _write_manifest(
        plugin_root,
        [(version, f"v{version.replace('.', '_')}") for version in ("1.0.0", "2.0.0", "3.0.0")],
        current="3.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        pending_installation=lambda _plugin_id: True,
    )

    outcome = harness.service.recycle_versions("DemoPlugin")

    assert outcome["removed"] == []
    assert set(outcome["kept"]) == {"1.0.0", "2.0.0", "3.0.0"}
    assert all((plugin_root / f"v{version.replace('.', '_')}").is_dir() for version in (
        "1.0.0", "2.0.0", "3.0.0"
    ))


def test_recycle_versions_propagates_referenced_version_collection_failures(tmp_path: Path):
    """收集引用集合失败时直接向上抛出，不能按空集继续回收。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    plugin_root = tmp_path / "demoplugin"
    _write_manifest(plugin_root, [("1.0.0", "v1_0_0")], current="1.0.0")
    harness = _Harness(
        plugins_root=tmp_path,
        instances_for_source_error=RuntimeError("实例存储不可用"),
    )

    with pytest.raises(RuntimeError, match="实例存储不可用"):
        harness.service.recycle_versions("DemoPlugin")

    # 收集失败时不能触发任何删除，版本目录必须原样保留。
    assert (plugin_root / "v1_0_0").is_dir()


# 四、批量回收调用方（PluginManager 逐插件隔离失败）


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器单例，测试后归还，避免污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_recycle_all_plugin_versions_skips_instances_and_isolates_failures(
    plugin_manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量回收跳过虚拟实例 ID，且单个插件失败不阻断其余插件的回收。"""
    monkeypatch.setattr(
        plugin_manager, "get_plugin_ids", lambda: ["PluginA", "PluginB", "CloneWork"]
    )
    clone_instance = PluginInstance(instance_id="CloneWork", source_plugin_id="PluginA")
    monkeypatch.setattr(
        plugin_manager,
        "get_plugin_instance",
        lambda plugin_id: clone_instance if plugin_id == "CloneWork" else None,
    )
    recycle_calls: list[str] = []

    def fake_recycle_plugin_versions(plugin_id: str) -> dict:
        """PluginB 的回收总是失败，其余插件按原样返回回收结果。"""
        recycle_calls.append(plugin_id)
        if plugin_id == "PluginB":
            raise RuntimeError("boom")
        return {"removed": [], "kept": {}}

    monkeypatch.setattr(
        plugin_manager, "recycle_plugin_versions", fake_recycle_plugin_versions
    )

    results = plugin_manager.recycle_all_plugin_versions()

    assert recycle_calls == ["PluginA", "PluginB"]
    assert results == {"PluginA": {"removed": [], "kept": {}}}


def test_overview_falls_back_to_the_persisted_name_when_a_clone_is_not_loaded(
    tmp_path: Path,
):
    """分身加载失败时仍要显示名称，不能因为类没注册就退成一行裸 ID。"""
    plugin_root = tmp_path / "demoplugin"
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_manifest(plugin_root, [("1.0.0", "v1_0_0")], current="1.0.0")
    work = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_name="工作实例",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": work},
        display_names={"DemoPlugin": "演示插件"},
    )

    overview = harness.service.overview("DemoPlugin")

    bindings = {item["instance_id"]: item for item in overview["instances"]}
    assert bindings["DemoPluginWork"]["plugin_name"] == "工作实例"
    assert bindings["DemoPlugin"]["plugin_name"] == "演示插件"
