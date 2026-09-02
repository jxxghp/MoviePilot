"""插件已装版本总览与实例版本绑定切换测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.binding import PluginVersionBinding
from app.runtime.extensions.plugin.version import (
    plugin_version_dir_name,
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


class _Harness:
    """组装 PluginVersionBinding 依赖并记录调用轨迹的测试脚手架。"""

    def __init__(
        self,
        *,
        plugins_root: Path,
        instances: dict[str, PluginInstance] | None = None,
        plugin_exists: bool = True,
        start_results: dict | None = None,
        multi_version_blockers: list[str] | None = None,
        running_ids: set[str] | None = None,
    ) -> None:
        self.plugins_root = plugins_root
        self.instances: dict[str, PluginInstance] = dict(instances or {})
        self.saved: list[PluginInstance] = []
        self.stopped: list[str] = []
        self.start_calls: list[tuple[str, str | None]] = []
        self._start_results = start_results or {}
        self._plugin_exists = plugin_exists
        self._multi_version_blockers_result = (
            [] if multi_version_blockers is None else multi_version_blockers
        )
        self.multi_version_blockers_calls: list[tuple[str, list[Path]]] = []
        self._running_ids = running_ids or set()
        self.logger = _logger()
        self.service = PluginVersionBinding(
            plugins_root=plugins_root,
            plugin_exists=lambda _plugin_id: self._plugin_exists,
            get_instance=self.instances.get,
            instances_for_source=self._instances_for_source,
            save_instance=self._save_instance,
            running=lambda: {plugin_id: object() for plugin_id in self._running_ids},
            start=self._start,
            stop=self.stopped.append,
            multi_version_blockers=self._multi_version_blockers,
            log=self.logger,
        )

    def _instances_for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        return [
            instance
            for instance in self.instances.values()
            if instance.source_plugin_id == source_plugin_id
        ]

    def _save_instance(self, instance: PluginInstance) -> None:
        self.instances[instance.instance_id] = instance
        self.saved.append(instance)

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
    }
    assert bindings["DemoPluginHome"]["running"] is False
    assert bindings["DemoPluginHome"]["follow_current_version"] is True


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
    """实例不存在时直接返回失败，不产生任何副作用。"""
    harness = _Harness(plugins_root=tmp_path)

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
