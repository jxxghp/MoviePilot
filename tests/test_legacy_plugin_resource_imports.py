"""旧插件资源导入扫描与加载前准备合同测试。"""

from __future__ import annotations

import importlib
import os
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.runtime.compat import resource_imports
from app.runtime.compat.resource_imports import (
    PluginResourceImportScanError,
    RESOURCE_IMPORT_RULES,
    scan_plugin_resource_imports,
)
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.plugin_manager import PluginManager
from app.startup import plugins_initializer


# 版本化布局下用例插件的版本目录名
_FIXTURE_VERSION_DIR = "v1_0_0"

_HEADED_CLOAKBROWSER_ENTRYPOINTS = (
    "launch",
    "launch_async",
    "launch_context",
    "launch_context_async",
    "launch_persistent_context",
    "launch_persistent_context_async",
)


def _write_plugin(root: Path, plugin_id: str, source: str) -> Path:
    """写入一个仅用于 AST 扫描的最小插件源码目录。"""
    plugin_dir = root / plugin_id.lower()
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(source, encoding="utf-8")
    return plugin_dir


def _await_new_fs_tick(reference: os.stat_result, probe_dir: Path) -> None:
    """自旋等待，直到文件系统时间戳推进到一个新的可观测刻度。

    部分文件系统/内核对 inode 时间戳使用粗粒度时钟，间隔极短的两次写入可能
    落在同一刻度内、拿到完全相同的 mtime/ctime，使基于时间戳比较的缓存失效
    判断失去区分度、产生与执行顺序相关的偶发失败。通过反复触碰一个探测文件，
    直到其 ctime 晚于 reference，确保调用方紧随其后的写入必然落入新刻度。

    :param reference: 作为时间基准的 stat 结果
    :param probe_dir: 探测文件所在目录，测试结束后随 tmp_path 一并清理
    """
    probe = probe_dir / ".fs_tick_probe"
    deadline = time.monotonic() + 2.0
    while True:
        probe.write_text("x", encoding="utf-8")
        if probe.stat().st_ctime_ns > reference.st_ctime_ns:
            return
        if time.monotonic() > deadline:
            pytest.fail("等待文件系统时间戳刻度推进超时，无法验证 ctime 失效路径")
        time.sleep(0.001)


@pytest.mark.parametrize(
    "source",
    (
        "import cloakbrowser\n",
        "from cloakbrowser import *\n",
        "from cloakbrowser.browser import launch_context\n",
        "__import__('cloakbrowser')\n",
        "import importlib\nimportlib.import_module('cloakbrowser.browser')\n",
        "import importlib as loader\nloader.import_module('cloakbrowser')\n",
        "from importlib import import_module as load\nload('cloakbrowser')\n",
    ),
)
def test_cloakbrowser_import_shapes_require_display(
    tmp_path: Path,
    source: str,
) -> None:
    """静态、星号、子模块及常量动态导入均准备虚拟显示。"""
    plugin_dir = _write_plugin(tmp_path, "SamplePlugin", source)

    assert scan_plugin_resource_imports("SamplePlugin", plugin_dir) == ("host.display",)


@pytest.mark.parametrize(
    "entrypoint",
    _HEADED_CLOAKBROWSER_ENTRYPOINTS,
)
def test_all_headed_capable_cloakbrowser_entrypoints_require_display(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    """CloakBrowser 六类允许 headed 模式的入口共用同一资源规则。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "HeadedPlugin",
        f"from cloakbrowser import {entrypoint}\n",
    )

    assert scan_plugin_resource_imports("HeadedPlugin", plugin_dir) == ("host.display",)
    assert RESOURCE_IMPORT_RULES[0].headed_entrypoints == (
        _HEADED_CLOAKBROWSER_ENTRYPOINTS
    )


@pytest.mark.parametrize(
    ("plugin_id", "source"),
    (
        ("DynamicWechat", "from cloakbrowser import launch_context_async\n"),
        ("ContractCheck", "from cloakbrowser import launch_context\n"),
        ("InvitesSignin", "from cloakbrowser import launch_context\n"),
        (
            "WeatherWidget",
            "__import__('cloakbrowser')\nfrom cloakbrowser import launch_context\n",
        ),
        (
            "P115StrmHelper",
            "from cloakbrowser import launch_context as _cloak_launch_context\n",
        ),
    ),
)
def test_current_direct_cloakbrowser_plugin_shapes_require_display(
    tmp_path: Path,
    plugin_id: str,
    source: str,
) -> None:
    """当前五种直接 CloakBrowser 插件导入形态均命中 host.display。"""
    plugin_dir = _write_plugin(tmp_path, plugin_id, source)

    assert scan_plugin_resource_imports(plugin_id, plugin_dir) == ("host.display",)


def test_sdk_browser_import_does_not_require_legacy_resource(tmp_path: Path) -> None:
    """宿主 SDK 浏览器门面自行按 headless 参数协调资源，不应被保守扫描。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "SdkPlugin",
        "from app.sdk.browser import launch_browser_context_async\n",
    )

    assert scan_plugin_resource_imports("SdkPlugin", plugin_dir) == ()


def test_scanner_reuses_successful_file_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未变化源码在热加载扫描时复用按文件状态缓存的结果。"""
    plugin_dir = _write_plugin(tmp_path, "CachedPlugin", "import cloakbrowser\n")
    parse_calls = 0
    original_parse = resource_imports.ast.parse

    def count_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(resource_imports.ast, "parse", count_parse)

    assert scan_plugin_resource_imports("CachedPlugin", plugin_dir) == ("host.display",)
    assert scan_plugin_resource_imports("CachedPlugin", plugin_dir) == ("host.display",)
    assert parse_calls == 1


def test_scanner_invalidates_cache_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件大小或修改时间变化后重新解析，不沿用旧能力集合。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "ChangedPlugin",
        "from app.sdk.browser import launch_browser_context\n",
    )
    source_path = plugin_dir / "__init__.py"
    parse_calls = 0
    original_parse = resource_imports.ast.parse

    def count_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(resource_imports.ast, "parse", count_parse)

    assert scan_plugin_resource_imports("ChangedPlugin", plugin_dir) == ()
    source_path.write_text(
        "from cloakbrowser.browser import launch_persistent_context_async\n",
        encoding="utf-8",
    )
    assert scan_plugin_resource_imports("ChangedPlugin", plugin_dir) == (
        "host.display",
    )
    assert parse_calls == 2


def test_scanner_invalidates_equal_size_source_with_preserved_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等长热更新即使保留 mtime，也不能复用替换前的导入结论。"""
    plugin_dir = _write_plugin(tmp_path, "ReplacedPlugin", "import cloakbrowser\n")
    source_path = plugin_dir / "__init__.py"
    original_stat = source_path.stat()
    parse_calls = 0
    original_parse = resource_imports.ast.parse

    def count_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(resource_imports.ast, "parse", count_parse)

    assert scan_plugin_resource_imports("ReplacedPlugin", plugin_dir) == (
        "host.display",
    )
    # 保证接下来的写入必然落入新的文件系统时间戳刻度，让 ctime 的变化
    # 真实可观测，测试不再依赖执行顺序带来的偶然时序间隔。
    _await_new_fs_tick(original_stat, plugin_dir)
    source_path.write_text("import cloakbrowsex\n", encoding="utf-8")
    os.utime(
        source_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert scan_plugin_resource_imports("ReplacedPlugin", plugin_dir) == ()
    assert parse_calls == 2


def test_scanner_conservatively_prepares_resources_for_invalid_source(
    tmp_path: Path,
) -> None:
    """未被导入的残留语法文件不得阻断插件，但必须准备全部资源。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "BrokenPlugin",
        "from app.sdk.browser import launch_browser_context\n",
    )
    (plugin_dir / "unused.py").write_text(
        "from cloakbrowser import (\n",
        encoding="utf-8",
    )

    assert scan_plugin_resource_imports("BrokenPlugin", plugin_dir) == ("host.display",)


def test_scanner_conservatively_prepares_resources_for_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码读取失败时按全部资源准备，不能降级为空资源集合。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "UnreadablePlugin",
        "from app.sdk.browser import launch_browser_context\n",
    )

    original_open = resource_imports.tokenize.open

    def guarded_open(path: Path):
        if Path(path).parent == plugin_dir:
            raise OSError("fixture read failure")
        return original_open(path)

    monkeypatch.setattr(resource_imports.tokenize, "open", guarded_open)

    assert scan_plugin_resource_imports("UnreadablePlugin", plugin_dir) == (
        "host.display",
    )


def test_scanner_conservatively_prepares_resources_for_walk_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录遍历失败时准备全部资源，后续导入仍由 Python loader 判断。"""
    plugin_dir = _write_plugin(tmp_path, "WalkErrorPlugin", "plugin_name = 'ok'\n")

    def fail_walk(_path: Path, _pattern: str):
        raise OSError("fixture walk failure")

    monkeypatch.setattr(Path, "rglob", fail_walk)

    assert scan_plugin_resource_imports("WalkErrorPlugin", plugin_dir) == (
        "host.display",
    )


def test_scanner_honors_python_source_encoding_cookie(tmp_path: Path) -> None:
    """合法的非 UTF-8 Python 源码按 PEP 263 声明解析。"""
    plugin_dir = tmp_path / "encodedplugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_bytes(
        "# -*- coding: latin-1 -*-\n# café\nimport cloakbrowser\n".encode("latin-1")
    )

    assert scan_plugin_resource_imports("EncodedPlugin", plugin_dir) == (
        "host.display",
    )


def _write_versioned_plugin(root: Path, plugin_id: str, source: str) -> Path:
    """按版本化布局写入一个仅用于 AST 扫描的最小插件源码目录。

    :param root: 插件根目录
    :param plugin_id: 插件ID
    :param source: 主模块源码
    :return: 版本目录
    """
    version_dir = root / plugin_id.lower() / _FIXTURE_VERSION_DIR
    version_dir.mkdir(parents=True)
    (version_dir / "__init__.py").write_text(source, encoding="utf-8")
    return version_dir


def _plugin_id_of(module_name: str) -> str:
    """从版本化模块名中取出插件目录名。

    :param module_name: 形如 app.plugins.<插件ID>.<版本目录> 的模块名
    :return: 插件目录名
    """
    return module_name.split(".")[-2]


def _fake_plugin_module(module_name: str) -> ModuleType:
    """构造满足 PluginManager 类发现合同的内存模块。"""
    module = ModuleType(module_name)
    plugin_type = type(
        _plugin_id_of(module_name).title(),
        (),
        {
            "init_plugin": lambda self, _config: None,
            "plugin_name": "Fixture",
        },
    )
    setattr(module, plugin_type.__name__, plugin_type)
    return module


def test_plugin_preparer_runs_before_import_in_non_debug_and_isolates_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描或资源失败只阻止对应插件，后续插件仍按准备后导入的顺序加载。"""
    plugins_root = tmp_path / "app" / "plugins"
    for plugin_id in ("scanfailed", "resourcefailed", "healthy"):
        _write_versioned_plugin(plugins_root, plugin_id, "plugin_name = 'Fixture'\n")

    events: list[tuple[str, str]] = []

    def prepare(*, plugin_id: str, plugin_dir: Path) -> None:
        # 扫描范围是本次将被导入的那份源码，即版本目录
        assert plugin_dir.name == _FIXTURE_VERSION_DIR
        assert plugin_dir.parent.name == plugin_id
        events.append(("prepare", plugin_id))
        if plugin_id == "scanfailed":
            raise PluginResourceImportScanError("fixture scan failure")
        if plugin_id == "resourcefailed":
            raise RuntimeError("fixture resource activation failure")

    def import_plugin(module_name: str) -> ModuleType:
        plugin_id = _plugin_id_of(module_name)
        assert events[-1] == ("prepare", plugin_id)
        events.append(("import", plugin_id))
        return _fake_plugin_module(module_name)

    monkeypatch.setattr(
        plugin_manager_module,
        "settings",
        SimpleNamespace(ROOT_PATH=tmp_path, DEBUG=False),
    )
    monkeypatch.setattr(
        plugin_manager_module,
        "_legacy_plugin_import_preparer",
        prepare,
    )
    monkeypatch.setattr(
        plugin_manager_module,
        "_legacy_import_scanner",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(importlib, "import_module", import_plugin)

    plugins = PluginManager._load_selective_plugins(
        None,
        ["ScanFailed", "ResourceFailed", "Healthy"],
        lambda plugin_type: hasattr(plugin_type, "init_plugin"),
    )

    assert [plugin.__name__ for plugin in plugins] == ["Healthy"]
    assert ("prepare", "scanfailed") in events
    assert ("prepare", "resourcefailed") in events
    assert ("prepare", "healthy") in events
    assert ("import", "scanfailed") not in events
    assert ("import", "resourcefailed") not in events
    assert ("import", "healthy") in events


def test_startup_preparer_activates_scanner_results_generically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根逐项激活扫描结果，并使用稳定的旧插件导入原因。"""
    events: list[tuple[str, str, str]] = []
    plugin_dir = _write_plugin(tmp_path, "LegacyPlugin", "import cloakbrowser\n")
    monkeypatch.setattr(
        plugins_initializer,
        "scan_plugin_resource_imports",
        lambda plugin_id, path: (
            events.append(("scan", plugin_id, path.name))
            or ("host.display", "fixture.resource")
        ),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "acquire_managed_resource",
        lambda capability_id, *, reason: events.append(
            ("acquire", capability_id, reason)
        ),
    )

    plugins_initializer._prepare_legacy_plugin_import(
        plugin_id="LegacyPlugin",
        plugin_dir=plugin_dir,
    )

    assert events == [
        ("scan", "LegacyPlugin", "legacyplugin"),
        ("acquire", "host.display", "legacy_plugin_import"),
        ("acquire", "fixture.resource", "legacy_plugin_import"),
    ]
