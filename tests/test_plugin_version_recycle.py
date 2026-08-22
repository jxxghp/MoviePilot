"""插件版本目录回收：保留判据、保留窗口、删除安全校验与清单同步测试。

只覆盖 ``layout.recycle_plugin_version_directories`` 这一层纯函数行为，不依赖
数据库或 PluginManager；跟随实例期望版本受保护、批量回收单插件失败不阻断这两
条依赖实例绑定的行为在 tests/test_plugin_version_binding.py 里覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.extensions.lifecycle import layout as plugin_layout_module
from app.runtime.extensions.lifecycle.layout import (
    _delete_plugin_version_dir,
    plugin_version_dir_name,
    read_plugin_versions_manifest,
    recycle_plugin_version_directories,
    register_plugin_version,
    write_plugin_versions_manifest,
)


def _install_version(plugin_root: Path, version: str) -> Path:
    """在插件目录下落地一个最小版本目录并登记到已装版本清单。

    :param plugin_root: 插件源码根目录
    :param version: 版本号，登记后成为清单的当前版本
    :return: 落地的版本目录
    """
    dir_name = plugin_version_dir_name(version)
    version_dir = plugin_root / dir_name
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(
        f"plugin_version = {version!r}\n", encoding="utf-8"
    )
    register_plugin_version(plugin_root, version, dir_name, source="test")
    return version_dir


def _stamp_installed_at(plugin_root: Path, stamps: dict) -> None:
    """把已装版本清单里各版本的登记时间改写为指定值，消除真实时钟带来的顺序不确定性。

    :param plugin_root: 插件源码根目录
    :param stamps: 版本号到 ISO8601 时间字符串的映射
    """
    manifest = read_plugin_versions_manifest(plugin_root)
    for entry in manifest["versions"]:
        if entry["version"] in stamps:
            entry["installed_at"] = stamps[entry["version"]]
    write_plugin_versions_manifest(plugin_root, manifest["versions"], manifest["current"])


# 一、保留判据


def test_current_version_is_never_recycled(tmp_path: Path) -> None:
    """当前安装版本即使无实例引用、保留窗口为 0 也不删除。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")

    outcome = recycle_plugin_version_directories(
        plugin_root, referenced_versions=set(), keep_recent=0
    )

    assert outcome["removed"] == []
    assert outcome["kept"]["1.0.0"] == "当前安装版本"
    assert (plugin_root / "v1_0_0").is_dir()


def test_referenced_version_is_not_recycled(tmp_path: Path) -> None:
    """被实例引用的版本不删除，即便它既不是当前版本也不在保留窗口内。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")
    _install_version(plugin_root, "3.0.0")  # 当前版本

    outcome = recycle_plugin_version_directories(
        plugin_root, referenced_versions={"1.0.0"}, keep_recent=0
    )

    assert outcome["removed"] == ["2.0.0"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()
    assert not (plugin_root / "v2_0_0").exists()
    assert outcome["kept"]["1.0.0"].startswith("被实例引用")


def test_retention_window_keeps_the_n_most_recent_versions(tmp_path: Path) -> None:
    """保留窗口按登记时间保留最近 N 个版本，窗口外且无引用的旧版本被回收。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")
    _install_version(plugin_root, "3.0.0")
    _stamp_installed_at(
        plugin_root,
        {
            "1.0.0": "2020-01-01T00:00:00+00:00",
            "2.0.0": "2020-06-01T00:00:00+00:00",
            "3.0.0": "2021-01-01T00:00:00+00:00",
        },
    )

    outcome = recycle_plugin_version_directories(
        plugin_root, referenced_versions=set(), keep_recent=2
    )

    assert outcome["removed"] == ["1.0.0"]
    assert set(outcome["kept"]) == {"2.0.0", "3.0.0"}
    assert not (plugin_root / "v1_0_0").exists()
    assert (plugin_root / "v2_0_0").is_dir()
    assert (plugin_root / "v3_0_0").is_dir()


def test_missing_installed_at_is_treated_as_oldest(tmp_path: Path) -> None:
    """登记时间缺失的版本排到最旧，不占用保留窗口的名额。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")
    manifest = read_plugin_versions_manifest(plugin_root)
    for entry in manifest["versions"]:
        if entry["version"] == "1.0.0":
            entry.pop("installed_at", None)
    write_plugin_versions_manifest(plugin_root, manifest["versions"], manifest["current"])

    outcome = recycle_plugin_version_directories(
        plugin_root, referenced_versions=set(), keep_recent=1
    )

    assert outcome["removed"] == ["1.0.0"]
    assert set(outcome["kept"]) == {"2.0.0"}


def test_manifest_is_updated_after_recycling(tmp_path: Path) -> None:
    """回收后已装版本清单同步剔除被删除的版本条目，当前版本指针不变。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")

    recycle_plugin_version_directories(plugin_root, referenced_versions=set(), keep_recent=0)

    manifest = read_plugin_versions_manifest(plugin_root)
    assert {entry["version"] for entry in manifest["versions"]} == {"2.0.0"}
    assert manifest["current"] == "2.0.0"


def test_no_version_dirs_on_disk_is_a_no_op(tmp_path: Path) -> None:
    """磁盘上没有任何版本目录时直接返回空结果，不报错。"""
    plugin_root = tmp_path / "empty"
    plugin_root.mkdir()

    outcome = recycle_plugin_version_directories(plugin_root, referenced_versions=set())

    assert outcome == {"removed": [], "kept": {}}


# 二、删除失败隔离


def test_single_directory_delete_failure_does_not_block_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个版本目录删除失败不影响其余版本的回收。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")
    _install_version(plugin_root, "3.0.0")  # 当前版本，受保护

    original_rmtree = plugin_layout_module.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        """v1_0_0 的删除永远失败，其余目录按原样删除。"""
        if Path(path).name == "v1_0_0":
            raise OSError("boom")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(plugin_layout_module.shutil, "rmtree", flaky_rmtree)

    outcome = recycle_plugin_version_directories(
        plugin_root, referenced_versions=set(), keep_recent=0
    )

    assert outcome["removed"] == ["2.0.0"]
    assert (plugin_root / "v1_0_0").is_dir()
    assert not (plugin_root / "v2_0_0").exists()
    assert outcome["kept"]["1.0.0"] == "本次删除失败，下次回收重试"
    # 只删成功的版本才从清单摘除
    manifest = read_plugin_versions_manifest(plugin_root)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0", "3.0.0"}


# 三、删除前的安全校验（保留条目与路径逃逸）


@pytest.mark.parametrize("name", ["dist", "wheels", "__pycache__"])
def test_delete_helper_refuses_reserved_directory_names(tmp_path: Path, name: str) -> None:
    """dist/wheels/__pycache__ 等保留条目反解不出版本号，删除请求被拒绝。"""
    plugin_root = tmp_path / "sample"
    entry = plugin_root / name
    entry.mkdir(parents=True)

    deleted = _delete_plugin_version_dir(plugin_root, name, entry)

    assert deleted is False
    assert entry.is_dir()


def test_delete_helper_refuses_a_directory_name_mismatch(tmp_path: Path) -> None:
    """目录名反解出的版本号与待删除版本不一致时拒绝删除。"""
    plugin_root = tmp_path / "sample"
    version_dir = plugin_root / "v1_0_0"
    version_dir.mkdir(parents=True)

    deleted = _delete_plugin_version_dir(plugin_root, "2.0.0", version_dir)

    assert deleted is False
    assert version_dir.is_dir()


def test_delete_helper_refuses_a_directory_outside_the_plugin_root(tmp_path: Path) -> None:
    """目录 resolve() 后位于插件目录之外时拒绝删除，不触发 rmtree。"""
    plugin_root = tmp_path / "sample"
    plugin_root.mkdir()
    outside = tmp_path / "v9_9_9"
    outside.mkdir()
    (outside / "marker.txt").write_text("keep", encoding="utf-8")

    deleted = _delete_plugin_version_dir(plugin_root, "9.9.9", outside)

    assert deleted is False
    assert (outside / "marker.txt").exists()


def test_delete_helper_refuses_the_plugin_root_itself(tmp_path: Path) -> None:
    """待删除目录就是插件目录本身时拒绝删除，即便名字碰巧能反解出版本号。"""
    plugin_root = tmp_path / "v1_0_0"
    plugin_root.mkdir()
    (plugin_root / "marker.txt").write_text("keep", encoding="utf-8")

    deleted = _delete_plugin_version_dir(plugin_root, "1.0.0", plugin_root)

    assert deleted is False
    assert (plugin_root / "marker.txt").exists()


def test_recycle_leaves_reserved_entries_alone(tmp_path: Path) -> None:
    """完整回收流程中，保留条目不会被当成版本目录考虑或删除。"""
    plugin_root = tmp_path / "sample"
    _install_version(plugin_root, "1.0.0")
    _install_version(plugin_root, "2.0.0")
    for reserved in ("dist", "wheels", "__pycache__"):
        (plugin_root / reserved).mkdir()

    recycle_plugin_version_directories(plugin_root, referenced_versions=set(), keep_recent=0)

    for reserved in ("dist", "wheels", "__pycache__"):
        assert (plugin_root / reserved).is_dir()
