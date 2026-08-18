"""校验 app.sdk 是兼容清单 replacement 文案的受校验投影。

弃用告警把插件作者导向 ``app.sdk.X``，而运行期解析走的是 ``target``。两者是否落到
同一个对象、SDK 是否真的提供了被承诺的符号，只能由本文件的断言保证。
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    PACKAGE_EXPORTS,
    SYMBOL_ALIASES,
)
from app.sdk._exports import SDK_HOST_INTERNAL_EXPORTS, SDK_REQUIRED_EXPORTS
from app.testing.bootstrap import prepare_backend


PROJECT_ROOT = Path(__file__).parents[1]
SDK_PREFIX = "app.sdk"

prepare_backend()


def sdk_module_aliases() -> list[tuple[str, object]]:
    """
    收集 replacement 指向 SDK 模块的全部模块别名。

    :return: ``(旧模块路径, ModuleAlias)`` 列表
    """
    return sorted(
        (
            (legacy_name, alias)
            for legacy_name, alias in {**MODULE_ALIASES, **PACKAGE_ALIASES}.items()
            if alias.replacement.startswith(f"{SDK_PREFIX}.")
        ),
        key=lambda item: item[0],
    )


def sdk_symbol_aliases() -> list[tuple[str, str, object]]:
    """
    收集 replacement 指向 SDK 符号的全部符号别名。

    :return: ``(旧模块路径, 旧符号名, SymbolAlias)`` 列表
    """
    collected = []
    for owner, exports in (*PACKAGE_EXPORTS.items(), *SYMBOL_ALIASES.items()):
        for name, symbol in exports.items():
            if symbol.replacement.startswith(f"{SDK_PREFIX}."):
                collected.append((owner, name, symbol))
    return sorted(collected, key=lambda item: (item[0], item[1]))


REQUIRED_EXPORTS = sorted(
    (sdk_name, name, tuple(map(tuple, sources)))
    for sdk_name, symbols in SDK_REQUIRED_EXPORTS.items()
    for name, sources in symbols.items()
)


@pytest.mark.parametrize(
    ("sdk_name", "name", "sources"),
    REQUIRED_EXPORTS,
    ids=[f"{sdk_name}.{name}" for sdk_name, name, _ in REQUIRED_EXPORTS],
)
def test_sdk_provides_promised_symbol_from_canonical_source(sdk_name, name, sources):
    """SDK 必须提供清单承诺的符号，且与 canonical 来源是同一个对象。"""
    sdk_module = importlib.import_module(sdk_name)

    assert hasattr(sdk_module, name), (
        f"{sdk_name} 缺少清单承诺的符号 {name}，来源 {list(sources)}"
    )
    for source_module_name, source_name in sources:
        source_module = importlib.import_module(source_module_name)
        assert hasattr(source_module, source_name), (
            f"{source_module_name} 不再提供 {source_name}"
        )
        assert getattr(sdk_module, name) is getattr(source_module, source_name), (
            f"{sdk_name}.{name} 与 {source_module_name}.{source_name} 不是同一个对象"
        )


def test_every_sdk_replacement_module_is_importable():
    """清单里出现的每个 SDK 取用位置都必须真实存在。"""
    replacements = {alias.replacement for _, alias in sdk_module_aliases()}
    replacements.update(
        symbol.replacement.rpartition(".")[0] for _, _, symbol in sdk_symbol_aliases()
    )

    for replacement in sorted(replacements):
        importlib.import_module(replacement)


def test_sdk_symbol_replacements_resolve_to_canonical_objects():
    """符号别名承诺的 ``app.sdk.X.Name`` 必须与 target 解析结果一致。"""
    failures = []
    for owner, name, symbol in sdk_symbol_aliases():
        sdk_name, _, attribute = symbol.replacement.rpartition(".")
        sdk_module = importlib.import_module(sdk_name)
        target_module = importlib.import_module(symbol.target_module)
        expected = getattr(target_module, symbol.target_name)
        if not hasattr(sdk_module, attribute):
            failures.append(f"{owner}.{name} 承诺 {symbol.replacement}，SDK 无该符号")
        elif getattr(sdk_module, attribute) is not expected:
            failures.append(
                f"{owner}.{name} 承诺 {symbol.replacement}，"
                f"与 {symbol.target_module}.{symbol.target_name} 不是同一个对象"
            )

    assert not failures, "\n".join(failures)


def test_sdk_exports_never_shadow_canonical_objects():
    """SDK 公开的同名符号不得与 target 解析到的对象不同。"""
    failures = []
    for legacy_name, alias in sdk_module_aliases():
        sdk_module = importlib.import_module(alias.replacement)
        target_module = importlib.import_module(alias.target)
        for name in getattr(sdk_module, "__all__", ()):
            if not hasattr(target_module, name):
                continue
            if getattr(sdk_module, name) is not getattr(target_module, name):
                failures.append(
                    f"{legacy_name}：{alias.replacement}.{name} 与 "
                    f"{alias.target}.{name} 不是同一个对象"
                )

    assert not failures, "\n".join(failures)


def test_host_internal_symbols_stay_out_of_sdk_requirements():
    """宿主自用装配与生命周期入口不进入 SDK 必备导出。"""
    required_names = {
        (source_module_name, source_name)
        for _, _, sources in REQUIRED_EXPORTS
        for source_module_name, source_name in sources
    }

    for path in SDK_HOST_INTERNAL_EXPORTS:
        module_name, _, name = path.rpartition(".")
        assert (module_name, name) not in required_names, f"{path} 不应是 SDK 必备导出"


def test_sdk_export_projection_matches_manifest():
    """兼容清单对 SDK 的导出要求变化必须显式刷新生成清单。"""
    result = subprocess.run(
        [sys.executable, "scripts/sdk/exports.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_every_sdk_module_declares_public_surface():
    """插件可见的 SDK 模块必须显式声明 ``__all__``。"""
    for path in sorted((PROJECT_ROOT / "app" / "sdk").glob("*.py")):
        if path.stem.startswith("_"):
            continue
        module = importlib.import_module(f"{SDK_PREFIX}.{path.stem}")
        declared = getattr(module, "__all__", None)
        assert declared, f"{module.__name__} 未声明 __all__"
        for name in declared:
            assert hasattr(module, name), f"{module.__name__}.{name} 不可解析"
