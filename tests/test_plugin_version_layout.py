"""插件源码按版本分目录布局的映射、加载、清理与存量迁移测试。"""

from __future__ import annotations

import errno
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.lifecycle import layout as plugin_layout_module
from app.runtime.extensions.lifecycle.layout import (
    PLUGIN_FALLBACK_VERSION,
    PLUGIN_VERSIONS_MANIFEST_NAME,
    ensure_plugin_version_dir_available,
    migrate_legacy_plugin_layout,
    plugin_manifest_versions,
    plugin_module_name,
    plugin_version_dir_name,
    plugin_version_from_dir_name,
    read_plugin_versions_manifest,
    register_plugin_version,
    resolve_plugin_version_dir,
)
from app.runtime.extensions.plugin_manager import PluginManager

# 用 v2 生态的写法 from app.plugins import _PluginBase：被加载的是真插件，
# 这条路径由兼容层解析，一并覆盖挂载点形态下的旧写法
_PLUGIN_SOURCE_TEMPLATE = """
from app.plugins import _PluginBase


class {class_name}(_PluginBase):
    plugin_name = "{class_name}"
    plugin_version = "{version}"

    def init_plugin(self, config=None):
        pass
"""


def _write_version(
    plugins_root: Path,
    plugin_id: str,
    version: str,
    *,
    class_name: str = "SamplePlugin",
    extra_source: str = "",
    register: bool = True,
) -> Path:
    """在版本化布局下写入一个插件版本的最小源码。

    :param plugins_root: 插件根目录
    :param plugin_id: 插件目录名
    :param version: 版本号
    :param class_name: 插件主类名
    :param extra_source: 追加到主模块末尾的源码
    :param register: 是否把该版本登记为元信息中的当前版本
    :return: 版本目录
    """
    plugin_root = plugins_root / plugin_id
    dir_name = plugin_version_dir_name(version)
    version_dir = plugin_root / dir_name
    version_dir.mkdir(parents=True)
    source = _PLUGIN_SOURCE_TEMPLATE.format(class_name=class_name, version=version)
    (version_dir / "__init__.py").write_text(source + extra_source, encoding="utf-8")
    if register:
        register_plugin_version(plugin_root, version, dir_name, source="test")
    return version_dir


def _write_legacy(plugins_root: Path, plugin_id: str, source: str) -> Path:
    """写入一个存量平铺布局的插件源码目录。

    :param plugins_root: 插件根目录
    :param plugin_id: 插件目录名
    :param source: 主模块源码
    :return: 插件目录
    """
    plugin_root = plugins_root / plugin_id
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text(source, encoding="utf-8")
    return plugin_root


@pytest.fixture
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件根目录指向临时目录，并保证测试后不残留插件模块。

    :param tmp_path: 用例级临时目录
    :param monkeypatch: 用例级补丁器
    :return: 临时插件根目录
    """
    monkeypatch.setattr(
        plugin_manager_module,
        "settings",
        SimpleNamespace(ROOT_PATH=tmp_path, DEBUG=False),
    )
    root = tmp_path / "app" / "plugins"
    root.mkdir(parents=True)
    # 把临时插件根目录并入 app.plugins 包的搜索路径，使临时插件可被真实 import。
    # app.plugins 是命名空间包，其 __path__ 是只支持追加的 _NamespacePath，
    # 因此整体换成列表，退出时把原对象换回去
    plugins_package = importlib.import_module("app.plugins")
    original_path = plugins_package.__path__
    plugins_package.__path__ = [*original_path, str(root)]
    importlib.invalidate_caches()
    yield root
    plugins_package.__path__ = original_path
    for module_name in [name for name in sys.modules if name.startswith("app.plugins.")]:
        sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


# 一、版本号与目录名的映射


@pytest.mark.parametrize(
    "version, dir_name",
    [
        ("1.2.0", "v1_2_0"),
        ("2.0", "v2_0"),
        ("1.2.0-beta.1", "v1_2_0-beta_1"),
        ("1.0.0+build.5", "v1_0_0+build_5"),
        ("10.20.30-rc.1+exp.sha.5114f85", "v10_20_30-rc_1+exp_sha_5114f85"),
    ],
)
def test_version_dir_name_is_reversible(version: str, dir_name: str) -> None:
    """版本号到目录名的映射可逆，反解结果与原版本号一致。"""
    assert plugin_version_dir_name(version) == dir_name
    assert plugin_version_from_dir_name(dir_name) == version


@pytest.mark.parametrize(
    "dir_name",
    ["dist", "wheels", "__pycache__", "v", "v1.2.0", "1_2_0", ""],
)
def test_non_version_dir_names_are_not_reversed(dir_name: str) -> None:
    """非版本目录名不会被误解析为版本号。"""
    assert plugin_version_from_dir_name(dir_name) is None


@pytest.mark.parametrize("version", ["1_2_0", "1.2 .0", "1.2.0/x", "../x", "", "  "])
def test_non_semantic_versions_are_rejected(version: str) -> None:
    """非语义化版本号被拒绝，不做静默转换。"""
    with pytest.raises(ValueError):
        plugin_version_dir_name(version)


def test_case_insensitive_directory_collision_is_rejected(plugins_root: Path) -> None:
    """同插件两个版本的目录名仅大小写不同时拒绝安装。"""
    _write_version(plugins_root, "casing", "1.0.0-Beta", class_name="CasingPlugin")
    plugin_root = plugins_root / "casing"

    assert ensure_plugin_version_dir_available(plugin_root, "2.0.0") == "v2_0_0"
    with pytest.raises(ValueError):
        ensure_plugin_version_dir_available(plugin_root, "1.0.0-beta")


def test_plugin_directory_stays_a_namespace_package(plugins_root: Path) -> None:
    """插件目录这一层不放 __init__.py，保持命名空间包。"""
    _write_version(plugins_root, "nsplugin", "1.0.0", class_name="NsPlugin")

    assert not (plugins_root / "nsplugin" / "__init__.py").exists()
    module = importlib.import_module("app.plugins.nsplugin")
    assert getattr(module, "__file__", None) is None


# 二、多版本并存与按版本加载


def test_two_versions_coexist_and_load_by_version(plugins_root: Path) -> None:
    """两个版本目录并存时，按指定版本加载得到该版本的类。"""
    _write_version(plugins_root, "dual", "1.2.0", class_name="DualPlugin", register=False)
    _write_version(plugins_root, "dual", "2.0.0", class_name="DualPlugin", register=True)
    plugin_root = plugins_root / "dual"

    old_dir = resolve_plugin_version_dir(plugin_root, version="1.2.0")
    new_dir = resolve_plugin_version_dir(plugin_root, version="2.0.0")
    assert old_dir is not None and old_dir.name == "v1_2_0"
    assert new_dir is not None and new_dir.name == "v2_0_0"

    old_module = importlib.import_module(plugin_module_name(plugin_root, old_dir))
    new_module = importlib.import_module(plugin_module_name(plugin_root, new_dir))

    assert old_module.DualPlugin.plugin_version == "1.2.0"
    assert new_module.DualPlugin.plugin_version == "2.0.0"
    assert old_module.DualPlugin is not new_module.DualPlugin


def test_current_version_comes_from_the_manifest(plugins_root: Path) -> None:
    """不指定版本时按元信息登记的当前版本加载，而不是版本号最高的。"""
    _write_version(plugins_root, "pinned", "1.2.0", class_name="PinnedPlugin", register=False)
    _write_version(plugins_root, "pinned", "2.0.0", class_name="PinnedPlugin", register=False)
    plugin_root = plugins_root / "pinned"
    register_plugin_version(plugin_root, "1.2.0", "v1_2_0", source="test")

    assert resolve_plugin_version_dir(plugin_root).name == "v1_2_0"


def test_missing_manifest_falls_back_to_the_highest_version(plugins_root: Path) -> None:
    """元信息缺失时回落到磁盘上版本号最高的版本目录。"""
    _write_version(plugins_root, "fallback", "1.2.0", class_name="FallbackPlugin", register=False)
    _write_version(plugins_root, "fallback", "10.0.0", class_name="FallbackPlugin", register=False)

    assert resolve_plugin_version_dir(plugins_root / "fallback").name == "v10_0_0"


def test_manifest_directory_mismatch_prefers_the_manifest_version(plugins_root: Path) -> None:
    """目录名不是权威真值，与元信息版本号不一致时以元信息为准。"""
    _write_version(plugins_root, "drift", "1.2.0", class_name="DriftPlugin", register=False)
    plugin_root = plugins_root / "drift"
    (plugin_root / PLUGIN_VERSIONS_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "current": "1.2.0",
                "versions": [{"version": "1.2.0", "directory": "v9_9_9"}],
            }
        ),
        encoding="utf-8",
    )

    assert plugin_manifest_versions(plugin_root) == {"1.2.0": "v1_2_0"}
    assert resolve_plugin_version_dir(plugin_root).name == "v1_2_0"


def test_selective_loader_imports_the_current_version(plugins_root: Path) -> None:
    """加载器按元信息的当前版本导入插件类。"""
    _write_version(plugins_root, "loaded", "1.0.0", class_name="LoadedPlugin", register=False)
    _write_version(plugins_root, "loaded", "3.1.0", class_name="LoadedPlugin", register=True)

    plugins = PluginManager._load_selective_plugins(
        None,
        ["Loaded"],
        lambda plugin_type: hasattr(plugin_type, "init_plugin"),
    )

    assert [plugin.__name__ for plugin in plugins] == ["LoadedPlugin"]
    assert plugins[0].plugin_version == "3.1.0"


# 三、绝对自引用 import 响亮失败


def test_absolute_self_referential_import_fails_loudly(plugins_root: Path) -> None:
    """插件写绝对自引用 import 时加载直接 ModuleNotFoundError，不做静默降级。"""
    version_dir = _write_version(
        plugins_root,
        "selfref",
        "1.0.0",
        class_name="SelfRefPlugin",
        extra_source="\nfrom app.plugins.selfref.helper import VALUE\n",
    )
    (version_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    plugin_root = plugins_root / "selfref"
    source_dir = resolve_plugin_version_dir(plugin_root)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(plugin_module_name(plugin_root, source_dir))


def test_relative_self_referential_import_still_works(plugins_root: Path) -> None:
    """相对 import 是版本化布局下的正确写法，加载不受影响。"""
    version_dir = _write_version(
        plugins_root,
        "relref",
        "1.0.0",
        class_name="RelRefPlugin",
        extra_source="\nfrom .helper import VALUE\n",
    )
    (version_dir / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    plugin_root = plugins_root / "relref"

    module = importlib.import_module(
        plugin_module_name(plugin_root, resolve_plugin_version_dir(plugin_root))
    )

    assert module.VALUE == 7


# 四、模块缓存按版本清理


def test_clear_plugin_modules_with_version_keeps_sibling_versions(plugins_root: Path) -> None:
    """传版本时只清该版本，兄弟版本的模块对象与命名空间包条目仍在。"""
    _write_version(plugins_root, "cleaner", "1.0.0", class_name="CleanerPlugin", register=False)
    _write_version(plugins_root, "cleaner", "2.0.0", class_name="CleanerPlugin", register=False)
    plugin_root = plugins_root / "cleaner"
    for version in ("1.0.0", "2.0.0"):
        importlib.import_module(
            plugin_module_name(plugin_root, resolve_plugin_version_dir(plugin_root, version))
        )
    sibling = sys.modules["app.plugins.cleaner.v1_0_0"]

    PluginManager._clear_plugin_modules("Cleaner", version_dir="v2_0_0")

    assert "app.plugins.cleaner.v2_0_0" not in sys.modules
    assert sys.modules["app.plugins.cleaner.v1_0_0"] is sibling
    assert "app.plugins.cleaner" in sys.modules


def test_clear_plugin_modules_without_version_clears_the_whole_family(plugins_root: Path) -> None:
    """不传版本时保持清全族，含插件命名空间包条目本身。"""
    _write_version(plugins_root, "family", "1.0.0", class_name="FamilyPlugin", register=False)
    _write_version(plugins_root, "family", "2.0.0", class_name="FamilyPlugin", register=False)
    plugin_root = plugins_root / "family"
    for version in ("1.0.0", "2.0.0"):
        importlib.import_module(
            plugin_module_name(plugin_root, resolve_plugin_version_dir(plugin_root, version))
        )

    PluginManager._clear_plugin_modules("Family")

    assert not [name for name in sys.modules if name.startswith("app.plugins.family")]


def test_clear_all_plugin_modules_keeps_the_host_package(plugins_root: Path) -> None:
    """不传插件时清光全部插件模块，但宿主包 app.plugins 自身留在缓存里。

    宿主包是插件的容器而非插件。把它一并逐出，后续 ``from app.plugins import ...``
    会重新导入出另一个模块对象，命名空间包已扩展的搜索路径与模块级状态都跟着旧
    对象一起失联，持有旧对象的调用方与新导入方会各看各的。
    """
    _write_version(plugins_root, "hosted", "1.0.0", class_name="HostedPlugin", register=False)
    plugin_root = plugins_root / "hosted"
    importlib.import_module(
        plugin_module_name(plugin_root, resolve_plugin_version_dir(plugin_root, "1.0.0"))
    )
    host_package = sys.modules["app.plugins"]

    PluginManager._clear_plugin_modules()

    assert not [name for name in sys.modules if name.startswith("app.plugins.")]
    assert sys.modules["app.plugins"] is host_package


# 五、热重载路径解析


def test_hot_reload_resolves_plugin_and_version_from_path(plugins_root: Path) -> None:
    """版本目录下的文件变化能解析出插件主类名与所属版本目录。"""
    version_dir = _write_version(plugins_root, "watched", "1.4.2", class_name="WatchedPlugin")
    (version_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert PluginManager._get_plugin_target_from_path(version_dir / "helper.py") == (
        "WatchedPlugin",
        "v1_4_2",
    )
    assert PluginManager._get_plugin_id_from_path(version_dir / "__init__.py") == "WatchedPlugin"


def test_hot_reload_ignores_paths_outside_the_plugins_root(plugins_root: Path) -> None:
    """插件根目录以外的文件变化不解析为插件。"""
    assert PluginManager._get_plugin_target_from_path(plugins_root.parent / "main.py") is None


def test_hot_reload_still_resolves_unmigrated_legacy_layout(plugins_root: Path) -> None:
    """尚未迁移的存量平铺布局仍能解析出插件主类名，版本目录为空。"""
    plugin_root = _write_legacy(
        plugins_root,
        "legacywatch",
        _PLUGIN_SOURCE_TEMPLATE.format(class_name="LegacyWatchPlugin", version="1.0.0"),
    )

    assert PluginManager._get_plugin_target_from_path(plugin_root / "__init__.py") == (
        "LegacyWatchPlugin",
        None,
    )


# 六、静态资源路径


def test_remote_entry_url_carries_the_version_segment(plugins_root: Path) -> None:
    """联邦入口地址插入版本段，不同版本天然是不同 URL。"""
    _write_version(plugins_root, "federated", "1.2.0", class_name="FederatedPlugin")

    url = PluginManager.get_plugin_remote_entry("Federated", "dist/assets")

    assert url == "/plugin/file/federated/v1_2_0/dist/assets/remoteEntry.js"


def test_remote_entry_url_omits_the_version_segment_without_version_dir(plugins_root: Path) -> None:
    """解析不到版本目录时不插入版本段，回落到既有地址形态。"""
    assert (
        PluginManager.get_plugin_remote_entry("Missing", "dist/assets")
        == "/plugin/file/missing/dist/assets/remoteEntry.js"
    )


def test_static_file_base_dir_still_rejects_escape_with_version_segment(plugins_root: Path) -> None:
    """版本段只是 dist 路径的第一段，静态资源基目录仍是插件目录，逃逸仍被拒。

    静态资源路由用 ``plugins_root/<插件ID>`` 作基目录、``is_relative_to`` 做校验，
    版本段落在被校验的相对路径里，因此无需放宽任何安全校验。
    """
    _write_version(plugins_root, "guarded", "1.2.0", class_name="GuardedPlugin")
    base_dir = plugins_root / "guarded"
    secret = plugins_root / "other" / "secret.js"
    secret.parent.mkdir(parents=True)
    secret.write_text("x", encoding="utf-8")

    escaping = "v1_2_0/../../other/secret.js"
    assert ".." in escaping
    assert not (base_dir / escaping).resolve().is_relative_to(base_dir.resolve())

    legit = "v1_2_0/dist/assets/remoteEntry.js"
    assert (base_dir / legit).resolve().is_relative_to(base_dir.resolve())


# 七、存量布局迁移


def test_legacy_layout_is_migrated_into_a_version_dir(plugins_root: Path) -> None:
    """存量平铺布局按声明的版本号迁移到版本目录并写入元信息。"""
    plugin_root = _write_legacy(
        plugins_root,
        "legacy",
        _PLUGIN_SOURCE_TEMPLATE.format(class_name="LegacyPlugin", version="1.3.0"),
    )
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


def test_legacy_layout_without_version_uses_the_fallback_version(plugins_root: Path) -> None:
    """读不到版本号的存量插件按兜底版本号迁移，不阻断加载。"""
    plugin_root = _write_legacy(plugins_root, "noversion", "plugin_name = 'NoVersion'\n")

    migrated = migrate_legacy_plugin_layout(plugin_root)

    assert migrated == plugin_root / plugin_version_dir_name(PLUGIN_FALLBACK_VERSION)
    assert read_plugin_versions_manifest(plugin_root)["current"] == PLUGIN_FALLBACK_VERSION


def test_migration_is_idempotent(plugins_root: Path) -> None:
    """已迁移的插件再次迁移不做任何事。"""
    _write_version(plugins_root, "done", "1.0.0", class_name="DonePlugin")
    plugin_root = plugins_root / "done"
    before = read_plugin_versions_manifest(plugin_root)

    assert migrate_legacy_plugin_layout(plugin_root) is None
    assert read_plugin_versions_manifest(plugin_root) == before


def test_stray_entries_are_not_migrated_as_a_version(plugins_root: Path) -> None:
    """已迁移插件目录下的杂项条目不会被当成待迁移的存量版本。"""
    _write_version(plugins_root, "stray", "1.0.0", class_name="StrayPlugin")
    plugin_root = plugins_root / "stray"
    (plugin_root / ".DS_Store").write_text("x", encoding="utf-8")

    assert migrate_legacy_plugin_layout(plugin_root) is None
    assert not (plugin_root / plugin_version_dir_name(PLUGIN_FALLBACK_VERSION)).exists()
    assert resolve_plugin_version_dir(plugin_root).name == "v1_0_0"


def test_interrupted_migration_is_resumed(plugins_root: Path) -> None:
    """上次迁移中断留下的中转目录会被发现并续做。"""
    plugin_root = _write_legacy(
        plugins_root,
        "resumed",
        _PLUGIN_SOURCE_TEMPLATE.format(class_name="ResumedPlugin", version="2.5.0"),
    )
    staging = plugins_root / "resumed.migrating-deadbeef"
    os.rename(plugin_root, staging)

    migrated = migrate_legacy_plugin_layout(plugin_root)

    assert migrated == plugin_root / "v2_5_0"
    assert (plugin_root / "v2_5_0" / "__init__.py").is_file()
    assert not staging.exists()
    assert read_plugin_versions_manifest(plugin_root)["current"] == "2.5.0"


def test_cross_device_rename_abandons_migration(
    plugins_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨设备无法原子改名时放弃迁移，存量布局原样保留且仍可加载。"""
    plugin_root = _write_legacy(
        plugins_root,
        "crossdev",
        _PLUGIN_SOURCE_TEMPLATE.format(class_name="CrossDevPlugin", version="1.0.0"),
    )

    def _refuse(*_args, **_kwargs):
        """模拟跨设备改名失败。"""
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(plugin_layout_module.os, "rename", _refuse)

    assert migrate_legacy_plugin_layout(plugin_root) == plugin_root
    assert (plugin_root / "__init__.py").is_file()
    assert not (plugin_root / "v1_0_0").exists()
    assert not read_plugin_versions_manifest(plugin_root)
    assert list(plugins_root.iterdir()) == [plugin_root]


def test_loader_migrates_and_imports_legacy_layout(plugins_root: Path) -> None:
    """加载器遇到存量布局时先迁移再按版本目录导入。"""
    _write_legacy(
        plugins_root,
        "legacyload",
        _PLUGIN_SOURCE_TEMPLATE.format(class_name="LegacyLoadPlugin", version="4.0.1"),
    )

    plugins = PluginManager._load_selective_plugins(
        None,
        ["LegacyLoad"],
        lambda plugin_type: hasattr(plugin_type, "init_plugin"),
    )

    assert [plugin.__name__ for plugin in plugins] == ["LegacyLoadPlugin"]
    assert (plugins_root / "legacyload" / "v4_0_1" / "__init__.py").is_file()
    assert "app.plugins.legacyload.v4_0_1" in sys.modules
