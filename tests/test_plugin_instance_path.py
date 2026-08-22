"""插件实例路径入口的安全校验与历史数据迁移测试。"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.runtime.extensions.lifecycle.paths import plugin_instance_path
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


@pytest.fixture
def plugin_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """把插件数据根目录指向临时目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(
        plugin_paths_module,
        "settings",
        SimpleNamespace(PLUGIN_DATA_PATH=root),
    )
    return root


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

_UNSAFE_SEGMENTS = ["a/b", "a\\b", "..", ".", "C:", "\x00", ""]
_SAFE_SEGMENTS = ["插件分身", "Sample Plugin", "sample-plugin", "sample_plugin"]


@pytest.mark.parametrize("value", _UNSAFE_SEGMENTS)
def test_unsafe_plugin_id_is_rejected(
    plugin_data_root: Path,
    value: str,
) -> None:
    """插件ID为不安全取值时拒绝，且不留下任何目录。"""
    with pytest.raises(ValueError):
        plugin_instance_path(value, DEFAULT_INSTANCE_ID, "data")

    assert list(plugin_data_root.iterdir()) == []


@pytest.mark.parametrize("value", _UNSAFE_SEGMENTS)
def test_unsafe_instance_id_is_rejected(
    plugin_data_root: Path,
    value: str,
) -> None:
    """实例ID为不安全取值时拒绝，且不留下任何目录。"""
    with pytest.raises(ValueError):
        plugin_instance_path("SamplePlugin", value, "data")

    assert list(plugin_data_root.iterdir()) == []


@pytest.mark.parametrize("value", _SAFE_SEGMENTS)
def test_safe_plugin_id_is_accepted(plugin_data_root: Path, value: str) -> None:
    """插件ID为安全取值时建立数据目录。"""
    target = plugin_instance_path(value, DEFAULT_INSTANCE_ID, "data")

    assert target == plugin_data_root / value / "default" / "data"
    assert target.is_dir()


@pytest.mark.parametrize("value", _SAFE_SEGMENTS)
def test_safe_instance_id_is_accepted(plugin_data_root: Path, value: str) -> None:
    """实例ID为安全取值时建立数据目录。"""
    target = plugin_instance_path("SamplePlugin", value, "data")

    assert target == plugin_data_root / "SamplePlugin" / value / "data"
    assert target.is_dir()


def test_unsupported_kind_is_rejected(plugin_data_root: Path) -> None:
    """未实现的用途分类被拒绝。"""
    with pytest.raises(ValueError):
        plugin_instance_path("SamplePlugin", DEFAULT_INSTANCE_ID, "log")


# ---------------------------------------------------------------------------
# 历史数据迁移
# ---------------------------------------------------------------------------


def test_fresh_plugin_skips_migration(plugin_data_root: Path) -> None:
    """未使用过的插件直接建立实例目录，并写入完成哨兵。"""
    target = plugin_instance_path("FreshPlugin", DEFAULT_INSTANCE_ID, "data")

    assert target == plugin_data_root / "FreshPlugin" / "default" / "data"
    assert target.is_dir()
    sentinel = plugin_data_root / "FreshPlugin" / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert sentinel.is_file()


def test_legacy_layout_is_migrated_into_default_instance(
    plugin_data_root: Path,
) -> None:
    """存量扁平目录下的内容整体搬入默认实例目录，原有文件保持可用。"""
    legacy_root = plugin_data_root / "LegacyPlugin"
    legacy_root.mkdir()
    (legacy_root / "state.json").write_text('{"k": 1}', encoding="utf-8")
    (legacy_root / "sub").mkdir()
    (legacy_root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")

    target = plugin_instance_path("LegacyPlugin", DEFAULT_INSTANCE_ID, "data")

    assert target == plugin_data_root / "LegacyPlugin" / "default" / "data"
    assert (target / "state.json").read_text(encoding="utf-8") == '{"k": 1}'
    assert (target / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested"
    sentinel = plugin_data_root / "LegacyPlugin" / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert sentinel.is_file()
    # 迁移只搬整目录，不残留其它中转目录
    leftovers = [
        entry
        for entry in plugin_data_root.iterdir()
        if entry.name.startswith("LegacyPlugin.migrating-")
    ]
    assert leftovers == []


def test_migration_is_idempotent_on_second_call(plugin_data_root: Path) -> None:
    """已完成迁移的插件再次访问时不再改动目录内容。"""
    legacy_root = plugin_data_root / "RepeatPlugin"
    legacy_root.mkdir()
    (legacy_root / "state.json").write_text("v1", encoding="utf-8")

    first = plugin_instance_path("RepeatPlugin", DEFAULT_INSTANCE_ID, "data")
    sentinel = plugin_data_root / "RepeatPlugin" / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    sentinel_mtime = sentinel.stat().st_mtime_ns

    second = plugin_instance_path("RepeatPlugin", DEFAULT_INSTANCE_ID, "data")

    assert second == first
    assert sentinel.stat().st_mtime_ns == sentinel_mtime
    assert (second / "state.json").read_text(encoding="utf-8") == "v1"


def test_migration_resumes_after_interrupted_first_rename(
    plugin_data_root: Path,
) -> None:
    """插件目录已改名到中转目录但未完成落地时，续做剩余步骤。"""
    plugin_dir_name = "CrashedPlugin"
    staging = plugin_data_root / f"{plugin_dir_name}.migrating-deadbeef"
    staging.mkdir()
    (staging / "state.json").write_text("survived", encoding="utf-8")

    target = plugin_instance_path(plugin_dir_name, DEFAULT_INSTANCE_ID, "data")

    assert target == plugin_data_root / plugin_dir_name / "default" / "data"
    assert (target / "state.json").read_text(encoding="utf-8") == "survived"
    assert not staging.exists()
    sentinel = plugin_data_root / plugin_dir_name / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert sentinel.is_file()


def test_migration_resumes_after_interrupted_second_rename(
    plugin_data_root: Path,
) -> None:
    """插件目录已重建但中转目录还没落地为默认实例时，续做剩余步骤。"""
    plugin_dir_name = "HalfDonePlugin"
    plugin_root = plugin_data_root / plugin_dir_name
    plugin_root.mkdir()
    staging = plugin_data_root / f"{plugin_dir_name}.migrating-deadbeef"
    staging.mkdir()
    (staging / "state.json").write_text("still there", encoding="utf-8")

    target = plugin_instance_path(plugin_dir_name, DEFAULT_INSTANCE_ID, "data")

    assert target == plugin_root / "default" / "data"
    assert (target / "state.json").read_text(encoding="utf-8") == "still there"
    assert not staging.exists()
    sentinel = plugin_root / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert sentinel.is_file()


def test_cross_device_rename_abandons_migration(
    plugin_data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rename 因跨设备失败时放弃迁移，旧目录原样保留且不做复制删除。"""
    legacy_root = plugin_data_root / "CrossDevicePlugin"
    legacy_root.mkdir()
    (legacy_root / "state.json").write_text("kept", encoding="utf-8")

    def _raise_exdev(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", _raise_exdev)

    target = plugin_instance_path("CrossDevicePlugin", DEFAULT_INSTANCE_ID, "data")

    assert target == legacy_root
    assert (target / "state.json").read_text(encoding="utf-8") == "kept"
    sentinel = legacy_root / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert not sentinel.exists()
    leftovers = [
        entry
        for entry in plugin_data_root.iterdir()
        if entry.name.startswith("CrossDevicePlugin.migrating-")
    ]
    assert leftovers == []


def test_migration_failure_returns_usable_fallback_path(
    plugin_data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移改名失败时不阻断，返回仍可写入的旧目录并保留原有文件。"""
    legacy_root = plugin_data_root / "BrokenPlugin"
    legacy_root.mkdir()
    (legacy_root / "state.json").write_text("still usable", encoding="utf-8")

    def _raise_generic(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "rename", _raise_generic)

    target = plugin_instance_path("BrokenPlugin", DEFAULT_INSTANCE_ID, "data")

    assert target == legacy_root
    assert target.is_dir()
    assert (target / "state.json").read_text(encoding="utf-8") == "still usable"
    sentinel = legacy_root / plugin_paths_module._INSTANCE_LAYOUT_SENTINEL_NAME
    assert not sentinel.exists()
