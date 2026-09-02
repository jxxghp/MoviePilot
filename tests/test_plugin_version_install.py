"""插件版本注册、存量布局迁移与安装期多版本并存拒绝测试。"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.extensions.plugin import version as plugin_version_module
from app.runtime.extensions.plugin.version import (
    PLUGIN_FALLBACK_VERSION,
    migrate_legacy_plugin_layout,
    plugin_version_dir_name,
    read_plugin_versions_manifest,
    register_plugin_version,
)
from app.startup.composition.plugin import (
    _reject_incompatible_plugin_version_switch as reject_incompatible_plugin_version_switch,
)


def _write_flat_plugin(plugin_root: Path, *, class_name: str, version: str | None) -> None:
    """写入一个平铺布局的最小插件源码。

    :param plugin_root: 插件源码根目录
    :param class_name: 插件主类名
    :param version: 类体内声明的 plugin_version 值；为 None 时不声明版本号
    """
    plugin_root.mkdir(parents=True, exist_ok=True)
    version_line = f"    plugin_version = {version!r}\n" if version else ""
    (plugin_root / "__init__.py").write_text(
        f"class {class_name}:\n{version_line}    plugin_name = {class_name!r}\n",
        encoding="utf-8",
    )


# 一、版本注册


def test_register_plugin_version_writes_manifest_and_sets_current(tmp_path: Path) -> None:
    """注册一个版本后元信息登记该版本并置为当前版本，返回其版本目录名。"""
    plugin_root = tmp_path / "registered"

    dir_name = register_plugin_version(plugin_root, "1.2.0", source="local")

    assert dir_name == "v1_2_0"
    manifest = read_plugin_versions_manifest(plugin_root)
    assert manifest["current"] == "1.2.0"
    assert manifest["versions"] == [
        {
            "version": "1.2.0",
            "directory": "v1_2_0",
            "installed_at": manifest["versions"][0]["installed_at"],
            "source": "local",
        }
    ]


def test_register_plugin_version_replaces_existing_entry_for_the_same_version(
    tmp_path: Path,
) -> None:
    """重新注册同一版本号时替换旧条目，不产生重复记录。"""
    plugin_root = tmp_path / "reregistered"
    register_plugin_version(plugin_root, "1.0.0", source="local")

    register_plugin_version(plugin_root, "1.0.0", source="migrated")

    manifest = read_plugin_versions_manifest(plugin_root)
    assert len(manifest["versions"]) == 1
    assert manifest["versions"][0]["source"] == "migrated"


def test_register_plugin_version_keeps_other_versions_and_switches_current(
    tmp_path: Path,
) -> None:
    """注册第二个版本后两条记录并存，当前版本切到新注册的版本。"""
    plugin_root = tmp_path / "dual"
    register_plugin_version(plugin_root, "1.0.0", source="local")

    register_plugin_version(plugin_root, "2.0.0", source="local")

    manifest = read_plugin_versions_manifest(plugin_root)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0", "2.0.0"}
    assert manifest["current"] == "2.0.0"


def test_register_plugin_version_rejects_illegal_version(tmp_path: Path) -> None:
    """版本号非法时拒绝注册，不写入任何元信息。"""
    plugin_root = tmp_path / "illegal"

    with pytest.raises(ValueError):
        register_plugin_version(plugin_root, "1_0_0", source="local")

    assert read_plugin_versions_manifest(plugin_root) == {}


# 二、存量布局迁移


def test_legacy_layout_is_migrated_into_a_version_dir(tmp_path: Path) -> None:
    """存量平铺布局按声明的版本号迁移到版本目录并写入元信息。"""
    plugin_root = tmp_path / "legacy"
    _write_flat_plugin(plugin_root, class_name="LegacyPlugin", version="1.3.0")
    (plugin_root / "utils.py").write_text("VALUE = 3\n", encoding="utf-8")

    migrated = migrate_legacy_plugin_layout(plugin_root)

    assert migrated == plugin_root / "v1_3_0"
    assert (plugin_root / "v1_3_0" / "__init__.py").is_file()
    assert (plugin_root / "v1_3_0" / "utils.py").is_file()
    assert not (plugin_root / "__init__.py").exists()
    manifest = read_plugin_versions_manifest(plugin_root)
    assert manifest["current"] == "1.3.0"
    assert manifest["versions"][0]["directory"] == "v1_3_0"
    assert manifest["versions"][0]["source"] == "migrated"


def test_legacy_layout_without_declared_version_uses_the_fallback_version(
    tmp_path: Path,
) -> None:
    """读不到版本号的存量插件按兜底版本号迁移，不阻断迁移。"""
    plugin_root = tmp_path / "noversion"
    _write_flat_plugin(plugin_root, class_name="NoVersionPlugin", version=None)

    migrated = migrate_legacy_plugin_layout(plugin_root)

    assert migrated == plugin_root / plugin_version_dir_name(PLUGIN_FALLBACK_VERSION)
    assert read_plugin_versions_manifest(plugin_root)["current"] == PLUGIN_FALLBACK_VERSION


def test_migration_is_a_no_op_when_nothing_needs_migrating(tmp_path: Path) -> None:
    """已经是版本化布局、没有平铺源码也没有残留中转目录时，迁移不做任何事。"""
    plugin_root = tmp_path / "done"
    version_dir = plugin_root / "v1_0_0"
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text("class DonePlugin:\n    pass\n", encoding="utf-8")
    register_plugin_version(plugin_root, "1.0.0", source="local")
    before = read_plugin_versions_manifest(plugin_root)

    assert migrate_legacy_plugin_layout(plugin_root) is None
    assert read_plugin_versions_manifest(plugin_root) == before


def test_stray_entries_are_not_migrated_as_a_version(tmp_path: Path) -> None:
    """已迁移插件目录下的杂项条目不会被当成待迁移的存量版本。"""
    plugin_root = tmp_path / "stray"
    version_dir = plugin_root / "v1_0_0"
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text("class StrayPlugin:\n    pass\n", encoding="utf-8")
    register_plugin_version(plugin_root, "1.0.0", source="local")
    (plugin_root / ".DS_Store").write_text("x", encoding="utf-8")

    assert migrate_legacy_plugin_layout(plugin_root) is None
    assert not (plugin_root / plugin_version_dir_name(PLUGIN_FALLBACK_VERSION)).exists()


def test_interrupted_migration_is_resumed(tmp_path: Path) -> None:
    """上次迁移中断留下的中转目录会被发现并续做。"""
    plugin_root = tmp_path / "resumed"
    _write_flat_plugin(plugin_root, class_name="ResumedPlugin", version="2.5.0")
    staging = tmp_path / "resumed.migrating-deadbeef"
    os.rename(plugin_root, staging)

    migrated = migrate_legacy_plugin_layout(plugin_root)

    assert migrated == plugin_root / "v2_5_0"
    assert (plugin_root / "v2_5_0" / "__init__.py").is_file()
    assert not staging.exists()
    assert read_plugin_versions_manifest(plugin_root)["current"] == "2.5.0"


def test_cross_device_rename_abandons_migration_and_keeps_flat_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨设备无法原子改名时放弃迁移，存量布局原样保留，不做复制加删除的非原子回退。"""
    plugin_root = tmp_path / "crossdev"
    _write_flat_plugin(plugin_root, class_name="CrossDevPlugin", version="1.0.0")

    def _refuse(*_args: object, **_kwargs: object) -> None:
        """模拟跨设备改名失败。"""
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(plugin_version_module.os, "rename", _refuse)

    assert migrate_legacy_plugin_layout(plugin_root) == plugin_root
    assert (plugin_root / "__init__.py").is_file()
    assert not (plugin_root / "v1_0_0").exists()
    assert not read_plugin_versions_manifest(plugin_root)
    assert list(tmp_path.iterdir()) == [plugin_root]


# 三、安装期多版本并存拒绝


def test_first_version_install_is_not_rejected(tmp_path: Path) -> None:
    """插件此前未安装任何版本时，本地安装不受并存检查影响。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="1.0.0")

    rejection = reject_incompatible_plugin_version_switch(
        "DemoPlugin",
        tmp_path / "app" / "plugins" / "demoplugin",
        source_dir,
    )

    assert rejection is None


def test_same_version_resync_is_not_rejected(tmp_path: Path) -> None:
    """新旧声明版本号相同时不是在装第二个版本，不触发并存检查。"""
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "from app.plugins.demoplugin.utils import helper\n"
        "class DemoPlugin:\n    plugin_version = '1.0.0'\n",
        encoding="utf-8",
    )
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="1.0.0")

    rejection = reject_incompatible_plugin_version_switch("DemoPlugin", plugin_dir, source_dir)

    assert rejection is None


def test_version_switch_with_self_referential_import_is_rejected(tmp_path: Path) -> None:
    """已装版本存在自引用绝对导入时，切换到不同版本号被拒绝并给出可读原因。"""
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "from app.plugins.demoplugin.utils import helper\n"
        "class DemoPlugin:\n    plugin_version = '1.0.0'\n",
        encoding="utf-8",
    )
    (plugin_dir / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")

    rejection = reject_incompatible_plugin_version_switch("DemoPlugin", plugin_dir, source_dir)

    assert rejection is not None
    assert "自引用" in rejection
    assert "1.0.0" in rejection and "2.0.0" in rejection


def test_version_switch_with_shared_base_model_is_rejected(tmp_path: Path) -> None:
    """已装版本在宿主共享声明基类上建模时，切换到不同版本号被拒绝。"""
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        "from app.db import Base\n"
        "class MyData(Base):\n    pass\n"
        "class DemoPlugin:\n    plugin_version = '1.0.0'\n",
        encoding="utf-8",
    )
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")

    rejection = reject_incompatible_plugin_version_switch("DemoPlugin", plugin_dir, source_dir)

    assert rejection is not None
    assert "共享声明基类" in rejection


def test_version_switch_without_blockers_is_allowed(tmp_path: Path) -> None:
    """新旧版本都只用相对 import、不建模共享基类时，版本切换不被拒绝。"""
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    _write_flat_plugin(plugin_dir, class_name="DemoPlugin", version="1.0.0")
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")

    rejection = reject_incompatible_plugin_version_switch("DemoPlugin", plugin_dir, source_dir)

    assert rejection is None


# 四、包适配器接线：安装流程实际调用了注入的并存检查端口


def test_package_manager_defaults_to_a_no_op_version_switch_guard(tmp_path: Path) -> None:
    """未装配并存检查端口时按不拦截退化，保持今天的单版本覆盖安装行为。"""
    manager = PluginPackageManager(plugin_root=tmp_path / "app" / "plugins")

    assert (
        manager._version_switch_guard("DemoPlugin", tmp_path / "any", tmp_path / "other")
        is None
    )


def test_local_install_rejects_when_the_injected_guard_blocks_the_switch(
    tmp_path: Path,
) -> None:
    """本地安装在写入前调用注入的并存检查端口，命中拒绝时不触碰已装内容。"""
    calls: list[tuple[str, Path, Path]] = []

    def _reject(pid: str, plugin_dir: Path, source_dir: Path) -> str | None:
        """记录调用参数并总是拒绝，验证接入点确实转发到了注入端口。"""
        calls.append((pid, plugin_dir, source_dir))
        return "拒绝理由"

    plugins_root = tmp_path / "app" / "plugins"
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")
    source_port = Mock()
    source_port.parse_local_repo_url.return_value = "DemoPlugin"
    source_port.parse_local_repo_path.return_value = None
    source_port.parse_local_repo_package_version.return_value = None
    source_port.get_local_plugin_candidate.return_value = {"path": str(source_dir)}
    source_port.check_plugin_system_version.return_value = (True, "")
    manager = PluginPackageManager(
        source=source_port,
        plugin_root=plugins_root,
        version_switch_guard=_reject,
    )
    plugin_dir = plugins_root / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("stable", encoding="utf-8")

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert success is False
    assert message == "拒绝理由"
    assert calls and calls[0][0] == "DemoPlugin"
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "stable"
