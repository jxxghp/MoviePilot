"""插件源码按版本分目录布局的目录名映射、元信息读写与加载路径解析测试。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.loader import PluginLoader
from app.runtime.extensions.plugin.version import (
    PLUGIN_VERSIONS_MANIFEST_NAME,
    ensure_plugin_version_dir_available,
    plugin_manifest_versions,
    plugin_version_dir_name,
    plugin_version_dirs,
    plugin_version_from_dir_name,
    read_declared_plugin_version,
    read_plugin_versions_manifest,
    resolve_plugin_version_dir,
    write_plugin_versions_manifest,
)
from app.schemas.plugin import PluginInstance


def _logger() -> SimpleNamespace:
    """提供加载器测试所需的最小日志端口。"""
    return SimpleNamespace(
        debug=lambda *_args: None,
        info=lambda *_args: None,
        warning=lambda *_args: None,
        error=lambda *_args: None,
    )


def _make_loader(plugins_root: Path) -> PluginLoader:
    """构造只需最小日志端口的加载器实例。"""
    return PluginLoader(
        plugins_root=plugins_root,
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_logger(),
    )


def _write_version(
    plugins_root: Path,
    plugin_id: str,
    version: str,
    *,
    class_name: str,
    declared_version: str | None = None,
) -> Path:
    """在插件根目录下写入一个版本目录的最小可加载源码。

    :param plugins_root: 插件根目录
    :param plugin_id: 插件目录名
    :param version: 版本号，决定版本目录名
    :param class_name: 插件主类名
    :param declared_version: 类体内声明的 plugin_version 值，默认与 version 相同
    :return: 版本目录
    """
    plugin_root = plugins_root / plugin_id
    version_dir = plugin_root / plugin_version_dir_name(version)
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(
        f"class {class_name}:\n"
        f"    plugin_version = {declared_version or version!r}\n"
        "    def init_plugin(self, config=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    return version_dir


def _write_manifest(
    plugin_root: Path,
    entries: list[tuple[str, str]],
    current: str | None,
) -> None:
    """写入版本元信息文件。

    :param plugin_root: 插件源码根目录
    :param entries: (版本号, 目录名) 列表
    :param current: 当前生效版本号
    """
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


@pytest.fixture(autouse=True)
def _isolate_plugin_modules():
    """回收测试期间手动导入的临时插件模块，避免污染其它用例的模块缓存。"""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("app.plugins."):
            sys.modules.pop(name, None)


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
def test_version_dir_name_mapping_is_reversible(version: str, dir_name: str) -> None:
    """版本号到目录名的映射可逆，反解结果与原版本号一致。"""
    assert plugin_version_dir_name(version) == dir_name
    assert plugin_version_from_dir_name(dir_name) == version


@pytest.mark.parametrize(
    "dir_name",
    ["dist", "wheels", "__pycache__", "v", "v1.2.0", "1_2_0", ""],
)
def test_non_version_directory_names_are_not_reversed(dir_name: str) -> None:
    """非版本目录名不会被误解析为版本号。"""
    assert plugin_version_from_dir_name(dir_name) is None


@pytest.mark.parametrize("version", ["1_2_0", "1.2 .0", "1.2.0/x", "../x", "", "  "])
def test_non_semantic_version_numbers_are_rejected(version: str) -> None:
    """非语义化版本号被拒绝，不做静默转换。"""
    with pytest.raises(ValueError):
        plugin_version_dir_name(version)


def test_case_insensitive_directory_collision_is_rejected(tmp_path: Path) -> None:
    """同插件两个版本的目录名仅大小写不同时拒绝安装。"""
    _write_version(tmp_path, "casing", "1.0.0-Beta", class_name="CasingPlugin")
    plugin_root = tmp_path / "casing"

    assert ensure_plugin_version_dir_available(plugin_root, "2.0.0") == "v2_0_0"
    with pytest.raises(ValueError):
        ensure_plugin_version_dir_available(plugin_root, "1.0.0-beta")


def test_plugin_version_dirs_lists_only_version_directories(tmp_path: Path) -> None:
    """扫描结果只包含能反解为版本号的目录，忽略杂项条目。"""
    plugin_root = tmp_path / "scanned"
    (plugin_root / "v1_0_0").mkdir(parents=True)
    (plugin_root / "dist").mkdir()
    (plugin_root / PLUGIN_VERSIONS_MANIFEST_NAME).write_text("{}", encoding="utf-8")

    assert set(plugin_version_dirs(plugin_root)) == {"1.0.0"}


def test_plugin_version_dirs_is_empty_when_root_is_missing(tmp_path: Path) -> None:
    """插件从未安装时，插件根目录不存在，扫描结果为空字典。"""
    assert plugin_version_dirs(tmp_path / "never_installed") == {}


# 二、多版本并存与解析


def test_two_versions_coexist_and_resolve_independently_by_version(
    tmp_path: Path,
) -> None:
    """两个版本目录并存时，指定版本号各自解析到对应的版本目录。"""
    _write_version(tmp_path, "dual", "1.2.0", class_name="DualPlugin")
    _write_version(tmp_path, "dual", "2.0.0", class_name="DualPlugin")
    plugin_root = tmp_path / "dual"

    old_dir = resolve_plugin_version_dir(plugin_root, version="1.2.0")
    new_dir = resolve_plugin_version_dir(plugin_root, version="2.0.0")

    assert old_dir.name == "v1_2_0"
    assert new_dir.name == "v2_0_0"


def test_resolving_an_uninstalled_version_raises(tmp_path: Path) -> None:
    """请求一个磁盘上不存在的版本时报错，不静默换成其它版本。"""
    _write_version(tmp_path, "partial", "1.0.0", class_name="PartialPlugin")
    plugin_root = tmp_path / "partial"

    with pytest.raises(ValueError):
        resolve_plugin_version_dir(plugin_root, version="9.9.9")


def test_current_version_comes_from_the_manifest_not_the_highest(
    tmp_path: Path,
) -> None:
    """不指定版本时按元信息登记的当前版本加载，而不是版本号最高的。"""
    _write_version(tmp_path, "pinned", "1.2.0", class_name="PinnedPlugin")
    _write_version(tmp_path, "pinned", "2.0.0", class_name="PinnedPlugin")
    plugin_root = tmp_path / "pinned"
    _write_manifest(
        plugin_root, [("1.2.0", "v1_2_0"), ("2.0.0", "v2_0_0")], current="1.2.0"
    )

    assert resolve_plugin_version_dir(plugin_root).name == "v1_2_0"


def test_missing_manifest_falls_back_to_the_highest_installed_version(
    tmp_path: Path,
) -> None:
    """元信息文件缺失时回落到磁盘上版本号最高的版本目录。"""
    _write_version(tmp_path, "fallback", "1.2.0", class_name="FallbackPlugin")
    _write_version(tmp_path, "fallback", "10.0.0", class_name="FallbackPlugin")

    assert resolve_plugin_version_dir(tmp_path / "fallback").name == "v10_0_0"


def test_manifest_current_missing_on_disk_falls_back_to_the_highest_version(
    tmp_path: Path,
) -> None:
    """元信息登记的当前版本在磁盘上已不存在时，回落到版本号最高的已装版本。"""
    _write_version(tmp_path, "stale", "1.2.0", class_name="StalePlugin")
    _write_version(tmp_path, "stale", "2.0.0", class_name="StalePlugin")
    plugin_root = tmp_path / "stale"
    _write_manifest(plugin_root, [("5.0.0", "v5_0_0")], current="5.0.0")

    assert resolve_plugin_version_dir(plugin_root).name == "v2_0_0"


def test_manifest_directory_mismatch_prefers_the_manifest_version(
    tmp_path: Path,
) -> None:
    """目录名不是权威真值，与元信息版本号不一致时以元信息推出的目录名为准。"""
    _write_version(tmp_path, "drift", "1.2.0", class_name="DriftPlugin")
    plugin_root = tmp_path / "drift"
    _write_manifest(plugin_root, [("1.2.0", "v9_9_9")], current="1.2.0")

    assert plugin_manifest_versions(plugin_root) == {"1.2.0": "v1_2_0"}
    assert resolve_plugin_version_dir(plugin_root).name == "v1_2_0"


def test_plugin_manifest_versions_ignores_entries_with_invalid_version(
    tmp_path: Path,
) -> None:
    """元信息中版本号非法或缺失的条目被忽略，不参与目录名推导。"""
    plugin_root = tmp_path / "malformed"
    write_plugin_versions_manifest(
        plugin_root,
        [
            {"version": "1_2_0", "directory": "v1_2_0"},
            {"directory": "v2_0_0"},
            {"version": "2.0.0", "directory": "v2_0_0"},
        ],
        current="2.0.0",
    )

    assert plugin_manifest_versions(plugin_root) == {"2.0.0": "v2_0_0"}


def test_resolve_falls_back_to_the_plugin_root_without_any_version_directory(
    tmp_path: Path,
) -> None:
    """插件根目录下没有任何版本目录时按平铺布局回落到插件根目录本身。

    这是今天所有插件的现状：没有安装任何版本目录，加载行为必须与引入版本化
    布局之前逐字一致，不指定版本和指定版本都回落到同一个目录。
    """
    plugin_root = tmp_path / "flat"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text(
        "class FlatPlugin:\n    plugin_version = '1.0.0'\n", encoding="utf-8"
    )

    assert resolve_plugin_version_dir(plugin_root) == plugin_root
    assert resolve_plugin_version_dir(plugin_root, version="1.0.0") == plugin_root


# 三、加载器接线


def test_loader_imports_the_manifest_current_version(tmp_path: Path) -> None:
    """加载器在存在多个版本目录时按元信息登记的当前版本导入插件类。"""
    _write_version(tmp_path, "loaded", "1.0.0", class_name="LoadedPlugin")
    _write_version(tmp_path, "loaded", "3.1.0", class_name="LoadedPlugin")
    plugin_root = tmp_path / "loaded"
    _write_manifest(
        plugin_root, [("1.0.0", "v1_0_0"), ("3.1.0", "v3_1_0")], current="3.1.0"
    )

    plugins = _make_loader(tmp_path).load(
        None, ["Loaded"], lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert [plugin.__name__ for plugin in plugins] == ["LoadedPlugin"]
    assert plugins[0].plugin_version == "3.1.0"


def test_loader_still_imports_a_flat_layout_plugin_without_version_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有版本目录的存量插件仍按插件根目录直接导入，行为不受版本化布局影响。

    平铺布局下 ``resolve_plugin_version_dir`` 回落到插件根目录本身，加载器随即
    沿用标准包导入机制，因此需要把临时插件根目录并入 ``app.plugins`` 的命名空间
    包搜索路径，才能让 ``importlib.import_module`` 找到它。
    """
    plugins_package = importlib.import_module("app.plugins")
    monkeypatch.setattr(
        plugins_package, "__path__", [*plugins_package.__path__, str(tmp_path)]
    )
    plugin_root = tmp_path / "legacy"
    plugin_root.mkdir(parents=True)
    (plugin_root / "__init__.py").write_text(
        "class LegacyPlugin:\n"
        "    plugin_version = '1.0.0'\n"
        "    def init_plugin(self, config=None):\n"
        "        pass\n",
        encoding="utf-8",
    )

    plugins = _make_loader(tmp_path).load(
        None, ["Legacy"], lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert [plugin.__name__ for plugin in plugins] == ["LegacyPlugin"]


def test_load_instance_uses_the_resolved_current_version_directory(
    tmp_path: Path,
) -> None:
    """虚拟实例加载沿用版本解析结果，从当前版本目录取源码而不是插件根目录。"""
    _write_version(tmp_path, "versioned", "1.0.0", class_name="VersionedPlugin")
    _write_version(tmp_path, "versioned", "2.0.0", class_name="VersionedPlugin")
    plugin_root = tmp_path / "versioned"
    _write_manifest(
        plugin_root, [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")], current="2.0.0"
    )

    instance = PluginInstance(instance_id="VersionedWork", source_plugin_id="Versioned")
    plugins = _make_loader(tmp_path).load_instance(
        instance, lambda candidate: hasattr(candidate, "init_plugin")
    )

    assert len(plugins) == 1
    assert plugins[0].__name__ == "VersionedWork"
    assert plugins[0].plugin_version == "2.0.0"


def test_import_versioned_module_clears_module_cache_when_exec_fails(
    tmp_path: Path,
) -> None:
    """按版本目录导入模块执行失败时清除半成品缓存，不让后续加载命中坏对象。

    标准 importlib 在 ``exec_module`` 抛错时会移除已经预置的 ``sys.modules`` 键；
    按版本目录手动构造模块规格的分支必须复现同一行为，否则失败后的模块对象
    会残留在缓存里，后续加载直接拿到这个半成品。
    """
    plugin_root = tmp_path / "broken"
    version_dir = plugin_root / "v1_0_0"
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    module_name = "app.plugins.broken"

    with pytest.raises(RuntimeError):
        PluginLoader._import_versioned_module(module_name, version_dir)

    assert module_name not in sys.modules


# 四、元信息文件读写


def test_write_and_read_plugin_versions_manifest_round_trip(tmp_path: Path) -> None:
    """写入的版本元信息可以原样读回。"""
    plugin_root = tmp_path / "roundtrip"
    entries = [("1.0.0", "v1_0_0"), ("2.0.0", "v2_0_0")]
    _write_manifest(plugin_root, entries, current="2.0.0")

    manifest = read_plugin_versions_manifest(plugin_root)

    assert manifest["current"] == "2.0.0"
    assert manifest["plugin_id"] == "roundtrip"
    assert [entry["version"] for entry in manifest["versions"]] == ["1.0.0", "2.0.0"]


def test_read_plugin_versions_manifest_returns_empty_when_missing(
    tmp_path: Path,
) -> None:
    """元信息文件不存在时返回空字典，不抛出异常。"""
    assert read_plugin_versions_manifest(tmp_path / "absent") == {}


def test_read_plugin_versions_manifest_returns_empty_when_corrupt(
    tmp_path: Path,
) -> None:
    """元信息文件内容损坏时按未登记处理，返回空字典。"""
    plugin_root = tmp_path / "corrupt"
    plugin_root.mkdir(parents=True)
    (plugin_root / PLUGIN_VERSIONS_MANIFEST_NAME).write_text(
        "{not valid json", encoding="utf-8"
    )

    assert read_plugin_versions_manifest(plugin_root) == {}


# 五、声明版本号解析


def test_read_declared_plugin_version_extracts_the_class_attribute(
    tmp_path: Path,
) -> None:
    """从插件主类的 plugin_version 类属性静态解析出声明版本号。"""
    init_file = tmp_path / "__init__.py"
    init_file.write_text(
        "class SomePlugin:\n    plugin_version = '1.3.0'\n", encoding="utf-8"
    )

    assert read_declared_plugin_version(init_file) == "1.3.0"


def test_read_declared_plugin_version_returns_none_when_absent(
    tmp_path: Path,
) -> None:
    """插件主类没有声明 plugin_version 时返回 None。"""
    init_file = tmp_path / "__init__.py"
    init_file.write_text("class SomePlugin:\n    plugin_name = 'Some'\n", encoding="utf-8")

    assert read_declared_plugin_version(init_file) is None


def test_read_declared_plugin_version_returns_none_for_unparseable_source(
    tmp_path: Path,
) -> None:
    """源码存在语法错误或文件不存在时返回 None，不抛出异常。"""
    assert read_declared_plugin_version(tmp_path / "missing" / "__init__.py") is None

    broken_file = tmp_path / "broken.py"
    broken_file.write_text("class Broken(:\n", encoding="utf-8")
    assert read_declared_plugin_version(broken_file) is None
