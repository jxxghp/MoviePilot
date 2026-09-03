"""插件已装版本总览与实例版本绑定切换测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

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
        instances_for_source_error: Exception | None = None,
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
        self._instances_for_source_error = instances_for_source_error
        self.logger = _logger()
        self.service = PluginVersionBinding(
            plugins_root=plugins_root,
            plugin_exists=self._plugin_exists,
            get_instance=self.instances.get,
            instances_for_source=self._instances_for_source,
            save_instance=self._save_instance,
            get_host_instance=self.host_instances.get,
            save_host_instance=self._save_host_instance,
            running=lambda: {plugin_id: object() for plugin_id in self._running_ids},
            start=self._start,
            stop=self.stopped.append,
            multi_version_blockers=self._multi_version_blockers,
            log=self.logger,
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
        plugin_version="1.0.0",
        follow_current_version=False,
    )
    home = PluginInstance(instance_id="DemoPluginHome", source_plugin_id="DemoPlugin")
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": work, "DemoPluginHome": home},
        running_ids={"DemoPluginWork"},
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
        "plugin_version": "1.0.0",
        "follow_current_version": False,
        "running": True,
        "is_host": False,
        "is_default_target": False,
    }
    assert bindings["DemoPluginHome"]["running"] is False
    assert bindings["DemoPluginHome"]["follow_current_version"] is True
    assert bindings["DemoPluginHome"]["is_host"] is False
    assert bindings["DemoPluginHome"]["is_default_target"] is False
    assert bindings["DemoPlugin"] == {
        "instance_id": "DemoPlugin",
        "plugin_version": None,
        "follow_current_version": True,
        "running": False,
        "is_host": True,
        "is_default_target": False,
    }
    assert overview["instances"][0]["is_host"] is True


def test_overview_raises_lookup_error_for_unknown_plugin(tmp_path: Path):
    """插件不存在时抛出 LookupError，不返回空壳总览掩盖问题。"""
    harness = _Harness(plugins_root=tmp_path, plugin_exists=False)

    with pytest.raises(LookupError):
        harness.service.overview("Missing")


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
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
    )

    assert success is True
    assert message == "DemoPluginWork"
    assert harness.instances["DemoPluginWork"].follow_current_version is False
    assert harness.stopped == ["DemoPluginWork"]
    assert harness.start_calls == [("DemoPluginWork", "2.0.0")]
    assert harness.multi_version_blockers_calls == []


def test_set_instance_version_rejects_uninstalled_target(tmp_path: Path):
    """目标版本未安装时拒绝切换，不停止也不启动实例。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(plugins_root=tmp_path, instances={"DemoPluginWork": instance})

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version="9.9.9"
    )

    assert success is False
    assert "未安装版本 9.9.9" in message
    assert harness.stopped == []
    assert harness.start_calls == []


def test_set_instance_version_requires_target_when_not_following(tmp_path: Path):
    """不跟随当前版本却未指定目标版本时拒绝切换。"""
    instance = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    harness = _Harness(plugins_root=tmp_path, instances={"DemoPluginWork": instance})

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version=None
    )

    assert success is False
    assert "必须指定目标版本" in message


def test_set_instance_version_returns_failure_for_unknown_instance(tmp_path: Path):
    """实例不存在、且该 ID 也不是已知插件本体时直接返回失败，不产生任何副作用。"""
    harness = _Harness(plugins_root=tmp_path, known_plugin_ids=set())

    success, message = harness.service.set_instance_version(
        "Missing", follow_current_version=True
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
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
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
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
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
        plugin_version="1.0.0",
        follow_current_version=False,
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        start_results={"2.0.0": PluginRuntimeStatus.LOAD_FAILED, "1.0.0": PluginRuntimeStatus.ACTIVE},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
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
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
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
        plugin_version="1.0.0",
        follow_current_version=False,
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        start_results={
            "2.0.0": PluginRuntimeStatus.LOAD_FAILED,
            "1.0.0": PluginRuntimeStatus.LOAD_FAILED,
        },
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
    )

    assert success is False
    assert "同样失败" in message
    assert harness.start_calls == [
        ("DemoPluginWork", "2.0.0"),
        ("DemoPluginWork", "1.0.0"),
    ]


def test_set_instance_version_switch_to_follow_current_does_not_retry(tmp_path: Path):
    """切回跟随当前版本失败时按单次尝试语义处理，不做回退重试。"""
    instance = PluginInstance(
        instance_id="DemoPluginWork",
        source_plugin_id="DemoPlugin",
        plugin_version="1.0.0",
        follow_current_version=False,
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": instance},
        start_results={None: PluginRuntimeStatus.LOAD_FAILED},
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=True
    )

    assert success is False
    assert "跟随当前版本失败" in message
    assert harness.start_calls == [("DemoPluginWork", None)]
    assert harness.instances["DemoPluginWork"].follow_current_version is True


def test_set_instance_version_applies_to_source_plugin_host(tmp_path: Path):
    """instance_id 等于源插件 ID 且该插件存在时按本体解析，写回本体端口而非分身端口。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    harness = _Harness(plugins_root=tmp_path)

    success, message = harness.service.set_instance_version(
        "DemoPlugin", follow_current_version=False, plugin_version="2.0.0"
    )

    assert success is True
    assert message == "DemoPlugin"
    assert harness.saved == []
    assert harness.saved_hosts[-1].instance_id == "DemoPlugin"
    assert harness.saved_hosts[-1].mode == "host"
    assert harness.host_instances["DemoPlugin"].follow_current_version is False
    assert harness.stopped == ["DemoPlugin"]
    assert harness.start_calls == [("DemoPlugin", "2.0.0")]


def test_set_instance_version_updates_existing_host_binding(tmp_path: Path):
    """本体已有版本绑定记录时，切换沿用同一条记录原地更新，不当作新分身处理。"""
    _write_version_dir(tmp_path, "demoplugin", "1.0.0")
    _write_version_dir(tmp_path, "demoplugin", "2.0.0")
    existing_host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    harness = _Harness(plugins_root=tmp_path, host_instances={"DemoPlugin": existing_host})

    success, _message = harness.service.set_instance_version(
        "DemoPlugin", follow_current_version=True
    )

    assert success is True
    assert harness.host_instances["DemoPlugin"].follow_current_version is True
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
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": clone},
        host_instances={"DemoPlugin": pinned_host},
        multi_version_blockers=["存在自引用绝对导入"],
    )

    success, message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version="2.0.0"
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
        follow_current_version=False,
        plugin_version="1.0.0",
    )
    harness = _Harness(
        plugins_root=tmp_path,
        instances={"DemoPluginWork": clone},
        host_instances={"DemoPlugin": pinned_host},
        multi_version_blockers=["存在自引用绝对导入"],
    )

    success, _message = harness.service.set_instance_version(
        "DemoPluginWork", follow_current_version=False, plugin_version="1.0.0"
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
        plugin_version="1.0.0",
        follow_current_version=True,
    )
    harness = _Harness(plugins_root=tmp_path, instances={"DemoPluginWork": following})

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
        plugin_version="1.0.0",
        follow_current_version=True,
    )
    harness = _Harness(plugins_root=tmp_path, host_instances={"DemoPlugin": host})

    outcome = harness.service.recycle_versions("DemoPlugin")

    assert outcome["removed"] == ["2.0.0"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()
    assert (plugin_root / "v4_0_0").is_dir()
    assert not (plugin_root / "v2_0_0").exists()


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
