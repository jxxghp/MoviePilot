"""插件版本注册、存量布局迁移、安装期多版本并存拒绝与安装落盘版本目录测试。"""

from __future__ import annotations

import errno
import io
import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system.plugin import package as plugin_package_module
from app.adapters.system.plugin.package import PluginPackageManager
from app.runtime.extensions.plugin import version as plugin_version_module
from app.runtime.extensions.plugin.version import (
    PLUGIN_FALLBACK_VERSION,
    migrate_legacy_plugin_layout,
    plugin_version_dir_name,
    plugin_version_dirs,
    read_plugin_versions_manifest,
    register_plugin_version,
    remove_plugin_installed_version,
)
from app.startup.composition.plugin import (
    _register_plugin_install_version as register_plugin_install_version,
)
from app.startup.composition.plugin import (
    _reject_incompatible_plugin_version_switch as reject_incompatible_plugin_version_switch,
)
from app.startup.composition.plugin import (
    _resolve_plugin_install_target as resolve_plugin_install_target,
)
from app.startup.composition.plugin import (
    _rollback_plugin_install_version as rollback_plugin_install_version,
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


# 五、安装落盘版本目录


def _versioned_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source: object | None = None,
) -> tuple[PluginPackageManager, Path]:
    """构造接了真实版本目录解析与登记端口的包管理器，运行目录隔离在 tmp_path。

    :param monkeypatch: pytest monkeypatch 夹具
    :param tmp_path: 隔离运行目录的临时根
    :param source: 市场来源端口；为空时用一个空 Mock 占位
    :return: (包管理器, 插件根目录)
    """
    plugin_root = tmp_path / "app" / "plugins"
    settings = SimpleNamespace(
        ROOT_PATH=tmp_path,
        TEMP_PATH=tmp_path / "temp",
        CONFIG_PATH=tmp_path / "config",
        REPO_GITHUB_HEADERS=lambda repo: {},
    )
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.get_runtime_setting",
        lambda key: getattr(settings, key),
    )
    manager = PluginPackageManager(
        source=source or Mock(),
        plugin_root=plugin_root,
        version_switch_guard=reject_incompatible_plugin_version_switch,
        install_target_resolver=resolve_plugin_install_target,
        install_version_registrar=register_plugin_install_version,
        install_version_rollback=rollback_plugin_install_version,
    )
    return manager, plugin_root


def _local_source_port(source_dir: Path) -> Mock:
    """构造一个只声明本地安装所需方法的市场来源端口替身。"""
    source_port = Mock()
    source_port.parse_local_repo_url.return_value = "DemoPlugin"
    source_port.parse_local_repo_path.return_value = None
    source_port.parse_local_repo_package_version.return_value = None
    source_port.get_local_plugin_candidate.return_value = {"path": str(source_dir)}
    source_port.check_plugin_system_version.return_value = (True, "")
    return source_port


def _zip_bytes(files: dict[str, str]) -> bytes:
    """把文件名到文本内容的映射打包为内存中的 zip 字节串。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_local_install_lands_in_version_directory_and_registers_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """本地安装把声明版本号的插件源码落到版本目录，并登记来源标签 local。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="1.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert (success, message) == (True, "")
    installed = plugin_root / "demoplugin" / "v1_0_0" / "__init__.py"
    assert installed.is_file()
    manifest = read_plugin_versions_manifest(plugin_root / "demoplugin")
    assert manifest["current"] == "1.0.0"
    assert manifest["versions"][0]["source"] == "local"


def test_release_install_lands_in_version_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """GitHub Release 制品安装把声明版本号的插件源码落到版本目录。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    release_tag = "DemoPlugin_v1.0.0"
    zip_bytes = _zip_bytes(
        {"__init__.py": "class DemoPlugin:\n    plugin_version = '1.0.0'\n"}
    )
    responses = iter(
        [
            SimpleNamespace(
                status_code=200,
                json=lambda: {"assets": [{"name": f"{release_tag.lower()}.zip", "id": 42}]},
            ),
            SimpleNamespace(status_code=200, content=zip_bytes),
        ]
    )
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__request_with_fallback",
        lambda *_args, **_kwargs: next(responses),
    )

    ok, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin",
        False,
        lambda staging_dir: manager._PluginPackageManager__install_from_release(
            "DemoPlugin", "owner/repo", release_tag, staging_dir
        ),
    )

    assert (ok, message) == (True, "")
    installed = plugin_root / "demoplugin" / "v1_0_0" / "__init__.py"
    assert installed.is_file()
    manifest = read_plugin_versions_manifest(plugin_root / "demoplugin")
    assert manifest["current"] == "1.0.0"
    assert manifest["versions"][0]["source"] == "market"


def test_filelist_install_lands_in_version_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """市场文件列表安装把声明版本号的插件源码落到版本目录。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    source_content = "class DemoPlugin:\n    plugin_version = '1.0.0'\n"
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__get_file_list",
        lambda *_args: (
            [{"path": "plugins/demoplugin/__init__.py", "download_url": "https://example.invalid/init"}],
            "",
        ),
    )
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__request_with_fallback",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, text=source_content),
    )

    ok, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin",
        False,
        lambda staging_dir: manager._PluginPackageManager__prepare_content_via_filelist_sync(
            "DemoPlugin", "owner/repo", None, staging_dir
        ),
    )

    assert (ok, message) == (True, "")
    installed = plugin_root / "demoplugin" / "v1_0_0" / "__init__.py"
    assert installed.is_file()
    assert installed.read_text(encoding="utf-8") == source_content
    manifest = read_plugin_versions_manifest(plugin_root / "demoplugin")
    assert manifest["current"] == "1.0.0"


@pytest.mark.asyncio
async def test_async_filelist_install_lands_in_version_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """异步市场文件列表安装同样把内容落到版本目录，与同步路径行为一致。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    source_content = "class DemoPlugin:\n    plugin_version = '1.0.0'\n"

    async def fake_get_file_list(*_args: object) -> tuple[list[dict[str, str]], str]:
        """返回一个只含单个源文件的伪造市场目录列表。"""
        return (
            [{"path": "plugins/demoplugin/__init__.py", "download_url": "https://example.invalid/init"}],
            "",
        )

    async def fake_request(*_args: object, **_kwargs: object) -> SimpleNamespace:
        """返回伪造的文件下载响应。"""
        return SimpleNamespace(status_code=200, text=source_content)

    monkeypatch.setattr(manager, "_PluginPackageManager__async_get_file_list", fake_get_file_list)
    monkeypatch.setattr(manager, "_PluginPackageManager__async_request_with_fallback", fake_request)

    async def prepare(staging_dir: Path) -> tuple[bool, str]:
        """把市场文件列表内容准备进给定的暂存目录。"""
        return await manager._PluginPackageManager__prepare_content_via_filelist_async(
            "DemoPlugin", "owner/repo", None, staging_dir
        )

    ok, message = await manager._PluginPackageManager__install_flow_async(
        "DemoPlugin", False, prepare,
    )

    assert (ok, message) == (True, "")
    installed = plugin_root / "demoplugin" / "v1_0_0" / "__init__.py"
    assert installed.is_file()


def test_install_without_declared_version_stays_flat_and_skips_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """读不到声明版本号时安装沿用平铺布局，不为其强行造版本目录或元信息。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version=None)
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert (success, message) == (True, "")
    assert (plugin_root / "demoplugin" / "__init__.py").is_file()
    assert not (plugin_root / "demoplugin" / "versions.json").exists()
    assert plugin_version_dirs(plugin_root / "demoplugin") == {}


def test_same_version_local_reinstall_is_idempotent_and_stays_flat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """已经是平铺布局时同版本重装保持平铺，不会为其凭空造出版本目录。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="1.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version="1.0.0")

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert (success, message) == (True, "")
    assert (existing_dir / "__init__.py").is_file()
    assert plugin_version_dirs(existing_dir) == {}
    assert not (existing_dir / "versions.json").exists()


def test_first_multi_version_local_install_migrates_legacy_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """首次安装出现第二个版本号时，先把存量平铺源码迁移进版本目录再写入新版本。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version="1.0.0")

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert (success, message) == (True, "")
    assert (existing_dir / "v1_0_0" / "__init__.py").is_file()
    assert (existing_dir / "v2_0_0" / "__init__.py").is_file()
    assert not (existing_dir / "__init__.py").exists()
    manifest = read_plugin_versions_manifest(existing_dir)
    assert manifest["current"] == "2.0.0"
    versions_by_number = {entry["version"]: entry for entry in manifest["versions"]}
    assert versions_by_number["1.0.0"]["source"] == "migrated"
    assert versions_by_number["2.0.0"]["source"] == "local"


def test_multi_version_local_install_blocked_keeps_old_version_loadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """写法体检命中阻断时拒绝安装，存量已装版本原样保留、不残留任何新目录。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="2.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    existing_dir.mkdir(parents=True)
    (existing_dir / "__init__.py").write_text(
        "from app.plugins.demoplugin.utils import helper\n"
        "class DemoPlugin:\n    plugin_version = '1.0.0'\n",
        encoding="utf-8",
    )
    (existing_dir / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")

    success, message = manager.install_local_raw("DemoPlugin", repo_url="local://demoplugin")

    assert success is False
    assert "自引用" in message
    assert (existing_dir / "__init__.py").read_text(encoding="utf-8").startswith(
        "from app.plugins.demoplugin.utils import helper"
    )
    assert (existing_dir / "utils.py").is_file()
    assert not (existing_dir / "v1_0_0").exists()
    assert not (existing_dir / "v2_0_0").exists()
    assert not (existing_dir / "versions.json").exists()


def test_install_rolls_back_when_dependency_install_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """依赖安装失败时插件根目录必须回滚到安装前内容，不留半份新版本。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version="1.0.0")
    (existing_dir / "marker.py").write_text("MARK = 1\n", encoding="utf-8")

    def failing_dependencies(*_args: object, **_kwargs: object) -> tuple[bool, bool, str]:
        """模拟依赖安装失败。"""
        return True, False, "依赖安装失败：模拟"

    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__install_dependencies_if_required",
        failing_dependencies,
    )

    def prepare_same_version(staging_dir: Path) -> tuple[bool, str]:
        """准备一份内容不同但版本号相同的替换内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="1.0.0")
        (staging_dir / "marker.py").write_text("MARK = 2\n", encoding="utf-8")
        return True, ""

    ok, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin", False, prepare_same_version,
    )

    assert ok is False
    assert message == "依赖安装失败：模拟"
    assert (existing_dir / "marker.py").read_text(encoding="utf-8") == "MARK = 1\n"
    assert (existing_dir / "__init__.py").is_file()


def test_install_rolls_back_when_target_resolver_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """目标目录解析端口失败时插件根目录必须回滚到安装前内容。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version="1.0.0")
    (existing_dir / "marker.py").write_text("MARK = 1\n", encoding="utf-8")

    def failing_resolver(*_args: object, **_kwargs: object) -> None:
        """模拟版本目录解析端口异常。"""
        raise RuntimeError("模拟解析失败")

    monkeypatch.setattr(manager, "_install_target_resolver", failing_resolver)

    def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备一份最小可用的替换内容。"""
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "__init__.py").write_text("class DemoPlugin:\n    pass\n", encoding="utf-8")
        return True, ""

    ok, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin", False, prepare,
    )

    assert ok is False
    assert "解析插件安装目标失败" in message
    assert (existing_dir / "marker.py").read_text(encoding="utf-8") == "MARK = 1\n"
    assert (existing_dir / "__init__.py").is_file()


def test_reinstalling_an_existing_version_directory_overwrites_it_idempotently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """重装已存在的版本目录直接覆盖该目录内容，元信息里的版本条目不重复。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_dir = existing_dir / "v1_0_0"
    _write_flat_plugin(version_dir, class_name="DemoPlugin", version="1.0.0")
    (version_dir / "marker.py").write_text("MARK = 1\n", encoding="utf-8")
    register_plugin_version(existing_dir, "1.0.0", source="local")

    def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备同一版本号但内容不同的重装内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="1.0.0")
        (staging_dir / "marker.py").write_text("MARK = 2\n", encoding="utf-8")
        return True, ""

    ok, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin", False, prepare, source_label="local",
    )

    assert (ok, message) == (True, "")
    assert (version_dir / "marker.py").read_text(encoding="utf-8") == "MARK = 2\n"
    manifest = read_plugin_versions_manifest(existing_dir)
    assert manifest["current"] == "1.0.0"
    assert len(manifest["versions"]) == 1


# 六、安装失败清理收敛到单个版本，不牵连插件的其它已装版本


def _register_real_version(plugin_root: Path, version: str, *, marker: str) -> Path:
    """在插件根目录下就地写出一个真实版本目录并登记进版本元信息，供回滚测试模拟已装版本。

    :param plugin_root: 插件源码根目录
    :param version: 版本号
    :param marker: 写入版本目录内 marker.py 的内容，用于断言该版本未被误删
    :return: 已写入的版本目录
    """
    version_dir = plugin_root / plugin_version_dir_name(version)
    _write_flat_plugin(version_dir, class_name="DemoPlugin", version=version)
    (version_dir / "marker.py").write_text(marker, encoding="utf-8")
    register_plugin_version(plugin_root, version, source="local")
    return version_dir


def test_remove_plugin_installed_version_keeps_sibling_versions_and_reverts_current(
    tmp_path: Path,
) -> None:
    """回滚失败版本只删该版本目录与元信息条目，其它已装版本与目录原样保留。"""
    plugin_root = tmp_path / "demoplugin"
    version_a = _register_real_version(plugin_root, "1.0.0", marker="A")
    version_b = _register_real_version(plugin_root, "2.0.0", marker="B")
    version_c = _register_real_version(plugin_root, "3.0.0", marker="C")
    assert read_plugin_versions_manifest(plugin_root)["current"] == "3.0.0"

    remove_plugin_installed_version(plugin_root, "3.0.0")

    assert not version_c.exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    manifest = read_plugin_versions_manifest(plugin_root)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0", "2.0.0"}
    assert manifest["current"] == "2.0.0"


def test_remove_plugin_installed_version_deletes_empty_plugin_root_after_only_version(
    tmp_path: Path,
) -> None:
    """被删版本是该插件唯一版本时，清理干净不留没有可用版本的空壳目录。"""
    plugin_root = tmp_path / "demoplugin"
    _register_real_version(plugin_root, "1.0.0", marker="A")

    remove_plugin_installed_version(plugin_root, "1.0.0")

    assert not plugin_root.exists()


def test_remove_plugin_installed_version_is_a_no_op_when_the_version_was_never_placed(
    tmp_path: Path,
) -> None:
    """要回滚的版本目录本就不存在（换入已回滚）时不报错，也不影响其它已装版本。"""
    plugin_root = tmp_path / "demoplugin"
    version_a = _register_real_version(plugin_root, "1.0.0", marker="A")

    remove_plugin_installed_version(plugin_root, "9.9.9")

    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    manifest = read_plugin_versions_manifest(plugin_root)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0"}
    assert manifest["current"] == "1.0.0"


def test_sync_install_failure_without_backup_only_removes_the_new_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """同步安装第三个版本时依赖安装失败且跳过备份，清理只收敛到第三个版本，另两个版本及登记完好。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")

    def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备一份声明第三个版本号的替换内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="3.0.0")
        return True, ""

    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__install_dependencies_if_required",
        lambda *_args, **_kwargs: (True, False, "依赖安装失败：模拟"),
    )

    success, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin", True, prepare,
    )

    assert success is False
    assert message == "依赖安装失败：模拟"
    assert not (existing_dir / "v3_0_0").exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    manifest = read_plugin_versions_manifest(existing_dir)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0", "2.0.0"}
    assert manifest["current"] == "2.0.0"


@pytest.mark.asyncio
async def test_async_install_failure_without_backup_only_removes_the_new_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """异步安装路径的失败清理必须与同步路径一样只收敛到本次安装的新版本。

    本地来源的异步安装入口把整个同步流程原样丢进线程池执行，不会真正走到
    ``__install_flow_async``；直接驱动该私有方法才能实际覆盖异步流程自身
    的清理分支，与既有的 ``test_async_filelist_install_lands_in_version_directory``
    等测试同一手法。
    """
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")

    async def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备一份声明第三个版本号的替换内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="3.0.0")
        return True, ""

    async def failing_dependencies(*_args: object, **_kwargs: object) -> tuple[bool, bool, str]:
        """模拟异步依赖安装失败。"""
        return True, False, "依赖安装失败：模拟"

    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__async_install_dependencies_if_required",
        failing_dependencies,
    )

    success, message = await manager._PluginPackageManager__install_flow_async(
        "DemoPlugin", True, prepare,
    )

    assert success is False
    assert message == "依赖安装失败：模拟"
    assert not (existing_dir / "v3_0_0").exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    manifest = read_plugin_versions_manifest(existing_dir)
    assert {entry["version"] for entry in manifest["versions"]} == {"1.0.0", "2.0.0"}
    assert manifest["current"] == "2.0.0"


def test_sync_install_failure_without_backup_in_flat_layout_still_removes_whole_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """平铺布局（无版本目录）下失败清理行为保持与改动前一致：整根插件目录被清理。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="1.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version="1.0.0")
    (existing_dir / "marker.py").write_text("MARK = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__install_dependencies_if_required",
        lambda *_args, **_kwargs: (True, False, "依赖安装失败：模拟"),
    )

    success, message = manager.install_local_raw(
        "DemoPlugin", repo_url="local://demoplugin", force_install=True,
    )

    assert success is False
    assert message == "依赖安装失败：模拟"
    assert not existing_dir.exists()


# 七、换入未提交与换入已提交后失败的清理边界


def _fail_rename_from_directory(root: Path):
    """构造只让位于给定目录下的源路径改名失败（模拟 EXDEV）的 os.rename 替身，其余改名走真实实现。"""
    real_rename = os.rename

    def fake_rename(src: object, dst: object) -> None:
        """按源路径是否位于给定目录内决定是否伪造跨设备改名失败。"""
        if Path(str(src)).is_relative_to(root):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst)

    return fake_rename


def _fail_copytree_into_directory(root: Path, marker_name: str, message: str):
    """构造只让目标路径位于给定目录树内的 copytree 调用失败的替身，先落半份新内容再抛错。"""
    real_copytree = shutil.copytree

    def fake_copytree(src: object, dst: object, **kwargs: object) -> None:
        """按目标路径是否位于给定目录内决定是否伪造复制中途磁盘写满。"""
        if Path(str(dst)).is_relative_to(root):
            Path(str(dst)).mkdir(parents=True, exist_ok=True)
            (Path(str(dst)) / marker_name).write_text("partial", encoding="utf-8")
            raise OSError(errno.ENOSPC, message)
        real_copytree(src, dst, **kwargs)

    return fake_copytree


def _patch_swap_to_fail_writing_into(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plugin_root: Path,
) -> None:
    """让暂存目录改名失败并让退化复制在写入插件根目录时中途失败，复现换入失败场景。"""
    monkeypatch.setattr(
        plugin_package_module.os,
        "rename",
        _fail_rename_from_directory(tmp_path / "temp" / "plugin_install_staging"),
    )
    monkeypatch.setattr(
        plugin_package_module.shutil,
        "copytree",
        _fail_copytree_into_directory(plugin_root, "partial.txt", "No space left on device"),
    )


def test_sync_install_swap_failure_in_flat_layout_leaves_directory_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """平铺布局强制安装换入失败时插件目录必须逐字节完好，不被失败清理二次删除。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version=None)
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    _write_flat_plugin(existing_dir, class_name="DemoPlugin", version=None)
    (existing_dir / "marker.py").write_text("MARK = 1\n", encoding="utf-8")
    init_before = (existing_dir / "__init__.py").read_text(encoding="utf-8")
    marker_before = (existing_dir / "marker.py").read_text(encoding="utf-8")

    _patch_swap_to_fail_writing_into(monkeypatch, tmp_path, plugin_root)

    success, message = manager.install_local_raw(
        "DemoPlugin", repo_url="local://demoplugin", force_install=True,
    )

    assert success is False
    assert "写入插件内容失败" in message
    assert existing_dir.is_dir()
    assert (existing_dir / "__init__.py").read_text(encoding="utf-8") == init_before
    assert (existing_dir / "marker.py").read_text(encoding="utf-8") == marker_before
    assert not (existing_dir / "partial.txt").exists()


def test_sync_install_swap_failure_installing_new_version_leaves_existing_versions_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """版本化布局强制安装新版本换入失败时，已装版本、清单与目标版本目录都回到安装前状态。"""
    source_dir = tmp_path / "repo" / "demoplugin"
    _write_flat_plugin(source_dir, class_name="DemoPlugin", version="3.0.0")
    manager, plugin_root = _versioned_manager(
        monkeypatch, tmp_path, source=_local_source_port(source_dir)
    )
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")
    manifest_before = read_plugin_versions_manifest(existing_dir)

    _patch_swap_to_fail_writing_into(monkeypatch, tmp_path, plugin_root)

    success, message = manager.install_local_raw(
        "DemoPlugin", repo_url="local://demoplugin", force_install=True,
    )

    assert success is False
    assert "写入插件内容失败" in message
    assert not (existing_dir / "v3_0_0").exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    assert read_plugin_versions_manifest(existing_dir) == manifest_before


@pytest.mark.asyncio
async def test_async_install_swap_failure_installing_new_version_leaves_existing_versions_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """异步安装路径下版本化布局换入失败同样不得清理已恢复的目录，与同步路径行为一致。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")
    manifest_before = read_plugin_versions_manifest(existing_dir)

    async def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备一份声明第三个版本号的替换内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="3.0.0")
        return True, ""

    _patch_swap_to_fail_writing_into(monkeypatch, tmp_path, plugin_root)

    success, message = await manager._PluginPackageManager__install_flow_async(
        "DemoPlugin", True, prepare,
    )

    assert success is False
    assert "写入插件内容失败" in message
    assert not (existing_dir / "v3_0_0").exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    assert read_plugin_versions_manifest(existing_dir) == manifest_before


def test_sync_install_swap_failure_reinstalling_existing_version_restores_original_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """重装已存在版本目录时换入失败，该版本必须恢复为换入前内容，不被失败清理连版本一起删除。

    这个场景才是新缺陷的真实复现：目标版本目录换入前已经存在真实内容，
    换入函数自身已经把它换回；旧的清理路径不区分“换入未提交”和“换入已
    提交后失败”，会把刚恢复好的这份内容连同版本登记一起删掉。
    """
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")
    marker_before = (version_b / "marker.py").read_text(encoding="utf-8")
    manifest_before = read_plugin_versions_manifest(existing_dir)

    def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备同一版本号但内容不同的重装内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="2.0.0")
        (staging_dir / "marker.py").write_text("MARK = 2-new\n", encoding="utf-8")
        return True, ""

    _patch_swap_to_fail_writing_into(monkeypatch, tmp_path, plugin_root)

    success, message = manager._PluginPackageManager__install_flow_sync(
        "DemoPlugin", True, prepare, source_label="local",
    )

    assert success is False
    assert "写入插件内容失败" in message
    assert version_b.is_dir()
    assert (version_b / "marker.py").read_text(encoding="utf-8") == marker_before
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert read_plugin_versions_manifest(existing_dir) == manifest_before


@pytest.mark.asyncio
async def test_async_install_cleans_up_new_version_when_registration_fails_after_successful_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """换入已经成功提交、版本元信息登记随后失败时，仍按既有语义清理本次安装写入的版本目录。"""
    manager, plugin_root = _versioned_manager(monkeypatch, tmp_path)
    existing_dir = plugin_root / "demoplugin"
    version_a = _register_real_version(existing_dir, "1.0.0", marker="A")
    version_b = _register_real_version(existing_dir, "2.0.0", marker="B")
    manifest_before = read_plugin_versions_manifest(existing_dir)

    async def prepare(staging_dir: Path) -> tuple[bool, str]:
        """准备一份声明第三个版本号的替换内容。"""
        _write_flat_plugin(staging_dir, class_name="DemoPlugin", version="3.0.0")
        return True, ""

    def failing_registrar(*_args: object, **_kwargs: object) -> None:
        """模拟版本元信息登记端口异常。"""
        raise RuntimeError("模拟登记失败")

    monkeypatch.setattr(manager, "_install_version_registrar", failing_registrar)

    success, message = await manager._PluginPackageManager__install_flow_async(
        "DemoPlugin", True, prepare,
    )

    assert success is False
    assert "登记插件版本元信息失败" in message
    assert not (existing_dir / "v3_0_0").exists()
    assert (version_a / "marker.py").read_text(encoding="utf-8") == "A"
    assert (version_b / "marker.py").read_text(encoding="utf-8") == "B"
    assert read_plugin_versions_manifest(existing_dir) == manifest_before
