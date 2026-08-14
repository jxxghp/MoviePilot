import builtins
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime.compat.diagnostics import (
    configure_legacy_import_diagnostics,
    get_legacy_import_diagnostics,
    reset_legacy_import_diagnostics,
    scan_plugin_legacy_imports,
)
from app.runtime.compat.imports import install_legacy_import_hook
from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    PACKAGE_EXPORTS,
    VIRTUAL_PACKAGES,
    ModuleAlias,
)


LEGACY_PACKAGE = "legacy_compat_test"
LEGACY_MODULE = f"{LEGACY_PACKAGE}.target"
CANONICAL_PACKAGE = "canonical_compat_test"
CANONICAL_MODULE = f"{CANONICAL_PACKAGE}.target"


@pytest.fixture
def compatibility_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """创建可计数初始化次数的临时 canonical 模块和旧路径映射。"""
    package_dir = tmp_path / CANONICAL_PACKAGE
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "target.py").write_text(
        "import builtins\n"
        "builtins._legacy_compat_test_count = "
        "getattr(builtins, '_legacy_compat_test_count', 0) + 1\n"
        "TOKEN = object()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(
        MODULE_ALIASES,
        LEGACY_MODULE,
        ModuleAlias(
            target=CANONICAL_MODULE,
            replacement="app.sdk.test",
            introduced="test",
            owner="test",
        ),
    )
    VIRTUAL_PACKAGES.add(LEGACY_PACKAGE)
    install_legacy_import_hook()
    reset_legacy_import_diagnostics()
    yield
    MODULE_ALIASES.pop(LEGACY_MODULE, None)
    VIRTUAL_PACKAGES.discard(LEGACY_PACKAGE)
    for module_name in (LEGACY_MODULE, LEGACY_PACKAGE, CANONICAL_MODULE, CANONICAL_PACKAGE):
        sys.modules.pop(module_name, None)
    if hasattr(builtins, "_legacy_compat_test_count"):
        delattr(builtins, "_legacy_compat_test_count")
    reset_legacy_import_diagnostics()


def test_legacy_import_reuses_canonical_module_identity(compatibility_modules):
    """旧路径先导入时应复用 canonical 模块且只执行一次源码。"""
    legacy = importlib.import_module(LEGACY_MODULE)
    canonical = importlib.import_module(CANONICAL_MODULE)

    assert legacy is canonical
    assert sys.modules[LEGACY_MODULE] is sys.modules[CANONICAL_MODULE]
    assert legacy.__name__ == CANONICAL_MODULE
    assert legacy.__spec__.name == CANONICAL_MODULE
    assert builtins._legacy_compat_test_count == 1


def test_canonical_first_import_keeps_same_legacy_identity(compatibility_modules):
    """canonical 路径先导入时，旧路径仍应绑定同一模块对象。"""
    canonical = importlib.import_module(CANONICAL_MODULE)
    legacy = importlib.import_module(LEGACY_MODULE)

    assert legacy is canonical
    assert legacy.TOKEN is canonical.TOKEN
    assert builtins._legacy_compat_test_count == 1


def test_virtual_package_blocks_unregistered_descendants(compatibility_modules):
    """合成旧父包不得向其他 Finder 泄漏未登记的子模块。"""
    package = importlib.import_module(LEGACY_PACKAGE)

    assert package.__path__ == []
    with pytest.raises(ModuleNotFoundError, match="未在兼容映射中登记"):
        importlib.import_module(f"{LEGACY_PACKAGE}.unknown")


def test_debug_diagnostics_warn_once_for_runtime_alias(compatibility_modules):
    """DEBUG 运行时兼容命中应输出一次包含迁移目标的警告。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)

    importlib.import_module(LEGACY_MODULE)
    importlib.import_module(LEGACY_MODULE)

    assert len(messages) == 1
    assert LEGACY_MODULE in messages[0]
    assert CANONICAL_MODULE in messages[0]
    assert "app.sdk.test" in messages[0]


def test_production_diagnostics_stay_silent(compatibility_modules):
    """DEBUG 关闭时兼容导入继续生效但不输出告警。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=False, emitter=messages.append)

    module = importlib.import_module(LEGACY_MODULE)

    assert module is importlib.import_module(CANONICAL_MODULE)
    assert messages == []


def test_plugin_scan_reports_cached_legacy_import(compatibility_modules, tmp_path: Path):
    """模块已缓存时，插件 AST 扫描仍应报告其静态旧导入。"""
    importlib.import_module(LEGACY_MODULE)
    reset_legacy_import_diagnostics()
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)
    plugin_dir = tmp_path / "sampleplugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        f"from {LEGACY_MODULE} import TOKEN\n",
        encoding="utf-8",
    )

    scan_plugin_legacy_imports("SamplePlugin", plugin_dir)
    scan_plugin_legacy_imports("SamplePlugin", plugin_dir)

    assert len(messages) == 1
    assert "插件 sampleplugin" in messages[0]
    assert "__init__.py:1" in messages[0]
    snapshot = get_legacy_import_diagnostics()
    assert ("app.plugins.sampleplugin", LEGACY_MODULE) in snapshot["reported"]


def test_plugin_scan_accepts_utf8_bom(compatibility_modules, tmp_path: Path):
    """插件源码带 UTF-8 BOM 时仍应识别旧导入并输出迁移警告。"""
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)
    plugin_dir = tmp_path / "bomplugin"
    plugin_dir.mkdir()
    source = f"from {LEGACY_MODULE} import TOKEN\n".encode("utf-8")
    (plugin_dir / "__init__.py").write_bytes(b"\xef\xbb\xbf" + source)

    scan_plugin_legacy_imports("BomPlugin", plugin_dir)

    assert len(messages) == 1
    assert "插件 bomplugin" in messages[0]
    assert LEGACY_MODULE in messages[0]


def test_manifest_aliases_reuse_real_canonical_modules():
    """正式映射表中的旧路径应在隔离进程中复用全部 canonical 模块。"""
    code = """
import importlib
from app.runtime.compat.manifest import MODULE_ALIASES
# CI 无 app.application.site.sites 二进制模块，先补垫片再校验全部映射（与 conftest 同源）。
from app.testing.bootstrap import ensure_sites_stub
ensure_sites_stub()

for legacy_name, alias in MODULE_ALIASES.items():
    try:
        canonical = importlib.import_module(alias.target)
    except ModuleNotFoundError:
        if alias.target == "app.application.site.sites":
            continue
        raise
    legacy = importlib.import_module(legacy_name)
    assert legacy is canonical, (legacy_name, alias.target)
    assert legacy.__name__ == alias.target, legacy_name
    assert legacy.__spec__.name == alias.target, legacy_name
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        check=True,
    )


def test_virtual_package_exports_resolve_exact_manifest_symbols():
    """合成旧包仅公开 manifest 声明的符号，并记录 DEBUG 兼容警告。"""
    legacy_package = "app.core.meta"
    sys.modules.pop(legacy_package, None)
    messages = []
    configure_legacy_import_diagnostics(enabled=True, emitter=messages.append)

    package = importlib.import_module(legacy_package)

    assert set(package.__all__) == set(PACKAGE_EXPORTS[legacy_package])
    assert package.MetaBase is importlib.import_module(
        "app.domain.meta.metabase"
    ).MetaBase
    assert PACKAGE_ALIASES[legacy_package].replacement in messages[0]
    reset_legacy_import_diagnostics()
