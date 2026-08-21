"""校验 app.sdk 是受校验的插件公开面快照。

弃用告警把插件作者导向 ``app.sdk.X``，而运行期解析走的是 ``target``。两者是否落到
同一个对象、SDK 是否真的提供了被承诺的符号，只能由本文件的断言保证。

清单与门面之间有两种独立失配：``app/sdk/_exports.py`` 相对源码陈旧，与门面未导出
清单承诺的符号。两者各由自己的用例判定，判据分别取自源码推导结果与门面实际属性，
任一方出错都不会遮蔽另一方。

快照的两个来源同样各由自己的用例判定：别名推导按兼容清单的 replacement 文案倒推，
只覆盖有旧路径的符号；门面自报按 SDK 各模块的 ``__all__`` 与 import 来源推导，覆盖
声明类这类没有任何旧路径别名的新出口。谁陈旧了谁报错，互不遮蔽。
"""
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    PACKAGE_EXPORTS,
    SYMBOL_ALIASES,
)
from app.sdk._exports import (
    SDK_DECLARED_EXPORTS,
    SDK_HOST_INTERNAL_EXPORTS,
    SDK_PLUGIN_BASE_SURFACE,
    SDK_REQUIRED_EXPORTS,
)
from app.testing.bootstrap import prepare_backend


PROJECT_ROOT = Path(__file__).parents[1]
SDK_PREFIX = "app.sdk"
GENERATOR_PATH = PROJECT_ROOT / "scripts" / "sdk" / "exports.py"
REGENERATE_HINT = "运行 python scripts/sdk/exports.py --write"
BINARY_MODULE = "app.application.site.sites"

prepare_backend()


def load_generator() -> ModuleType:
    """
    以模块形式载入 SDK 清单生成脚本，供用例直接取用其推导函数。

    :return: 生成脚本模块对象
    """
    spec = importlib.util.spec_from_file_location("sdk_exports_generator", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = load_generator()


def flatten(required: dict[str, dict[str, list]]) -> dict[tuple[str, str], tuple]:
    """
    把嵌套的导出要求摊平为可直接比较的映射。

    :param required: ``{SDK 模块: {符号: [(来源模块, 来源符号), ...]}}``
    :return: ``{(SDK 模块, 符号): ((来源模块, 来源符号), ...)}``
    """
    return {
        (sdk_name, name): tuple(sorted(map(tuple, sources)))
        for sdk_name, symbols in required.items()
        for name, sources in symbols.items()
    }


def facade_path(sdk_name: str) -> str:
    """
    返回 SDK 门面模块的仓库相对路径，用于报错时直指待改文件。

    :param sdk_name: SDK 模块全名
    :return: 形如 ``app/sdk/network.py`` 的相对路径
    """
    return f"app/sdk/{sdk_name.rpartition('.')[2]}.py"


LIVE_REQUIRED_EXPORTS = GENERATOR.collect_required_exports()
LIVE_EXPORTS = sorted(flatten(LIVE_REQUIRED_EXPORTS).items())
LIVE_DECLARED_EXPORTS = GENERATOR.collect_declared_exports()
DECLARED_EXPORTS = sorted(
    ((sdk_name, name), source)
    for sdk_name, symbols in LIVE_DECLARED_EXPORTS.items()
    for name, source in symbols.items()
)


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


def test_sdk_manifest_is_current():
    """源码推导出的导出要求变化后，生成清单必须同步刷新。"""
    live = flatten(LIVE_REQUIRED_EXPORTS)
    recorded = flatten(SDK_REQUIRED_EXPORTS)
    problems = []
    for sdk_name, name in sorted(live.keys() - recorded.keys()):
        sources = list(live[(sdk_name, name)])
        problems.append(f"源码新增、清单未登记：{sdk_name}.{name}（来源 {sources}）")
    for sdk_name, name in sorted(recorded.keys() - live.keys()):
        problems.append(f"清单残留、源码已无：{sdk_name}.{name}")
    for sdk_name, name in sorted(live.keys() & recorded.keys()):
        key = (sdk_name, name)
        if live[key] != recorded[key]:
            problems.append(
                f"来源已变更：{sdk_name}.{name} 清单记 {list(recorded[key])}，"
                f"源码为 {list(live[key])}"
            )
    if dict(GENERATOR.HOST_INTERNAL_EXPORTS) != dict(SDK_HOST_INTERNAL_EXPORTS):
        problems.append("宿主自用符号名单与生成脚本不一致")

    assert not problems, "\n".join(
        ["[清单陈旧] app/sdk/_exports.py 与源码推导结果不一致：", *problems, REGENERATE_HINT]
    )


def test_sdk_declared_manifest_is_current():
    """SDK 门面自报的导出变化后，生成清单必须同步刷新。

    别名推导覆盖不到没有旧路径的新出口，这一表就是它们唯一的登记处：改了来源、少了
    条目都会在这里显形，而不是等某个插件在运行期撞上 ImportError。
    """
    problems = []
    live = {
        (sdk_name, name): source
        for sdk_name, symbols in LIVE_DECLARED_EXPORTS.items()
        for name, source in symbols.items()
    }
    recorded = {
        (sdk_name, name): tuple(source)
        for sdk_name, symbols in SDK_DECLARED_EXPORTS.items()
        for name, source in symbols.items()
    }
    for sdk_name, name in sorted(live.keys() - recorded.keys()):
        problems.append(f"门面新增、清单未登记：{sdk_name}.{name}（来源 {live[(sdk_name, name)]}）")
    for sdk_name, name in sorted(recorded.keys() - live.keys()):
        problems.append(f"清单残留、门面已撤：{sdk_name}.{name}")
    for key in sorted(live.keys() & recorded.keys()):
        if live[key] != recorded[key]:
            problems.append(
                f"来源已变更：{key[0]}.{key[1]} 清单记 {recorded[key]}，源码为 {live[key]}"
            )

    assert not problems, "\n".join(
        ["[清单陈旧] SDK 门面自报的导出与 app/sdk/_exports.py 不一致：", *problems, REGENERATE_HINT]
    )


@pytest.mark.parametrize(
    ("key", "source"),
    DECLARED_EXPORTS,
    ids=[f"{sdk_name}.{name}" for (sdk_name, name), _ in DECLARED_EXPORTS],
)
def test_sdk_declared_export_binds_recorded_source(key, source):
    """门面自报的每个导出都必须解析到清单登记的那个对象。"""
    sdk_name, name = key
    source_module_name, source_name = source
    sdk_module = importlib.import_module(sdk_name)
    source_module = importlib.import_module(source_module_name)

    assert hasattr(sdk_module, name), (
        f"[门面缺符号] {sdk_name}.{name} 列入了 __all__ 却不可解析；"
        f"在 {facade_path(sdk_name)} 补 import"
    )
    assert getattr(sdk_module, name) is getattr(source_module, source_name), (
        f"[门面串对象] {sdk_name}.{name} 与 {source_module_name}.{source_name} 不是同一个对象"
    )


def test_plugin_base_surface_matches_the_pinned_snapshot():
    """扩展基类对扩展可见的公开面必须与登记快照一致。

    基类原样导出给插件，混在其中的冻结契约、扩展点与内部实现三层因此一并可见。快照
    不替这三层划界，它保证的是任何一层增删都要显式改一次登记，改动在评审里看得见。
    """
    live = GENERATOR.collect_plugin_base_surface()
    recorded = {kind: list(names) for kind, names in SDK_PLUGIN_BASE_SURFACE.items()}
    problems = []
    for kind in sorted({*live, *recorded}):
        added = sorted(set(live.get(kind, ())) - set(recorded.get(kind, ())))
        removed = sorted(set(recorded.get(kind, ())) - set(live.get(kind, ())))
        problems.extend(f"{kind} 新增：{name}" for name in added)
        problems.extend(f"{kind} 移除：{name}" for name in removed)

    assert not problems, "\n".join(
        [
            "[清单陈旧] _PluginBase 的公开面与 app/sdk/_exports.py 登记的快照不一致：",
            *problems,
            "移除即对存量插件的破坏性变更，新增即向插件多承诺一件事，两者都要确认后再刷新快照。",
            REGENERATE_HINT,
        ]
    )


@pytest.mark.parametrize(
    ("key", "sources"),
    LIVE_EXPORTS,
    ids=[f"{sdk_name}.{name}" for (sdk_name, name), _ in LIVE_EXPORTS],
)
def test_sdk_facade_exports_required_symbol(key, sources):
    """SDK 门面必须公开源码承诺的符号，且与 canonical 来源是同一个对象。"""
    sdk_name, name = key
    sdk_module = importlib.import_module(sdk_name)
    target = facade_path(sdk_name)

    assert hasattr(sdk_module, name), (
        f"[门面缺符号] {sdk_name} 未导出承诺的 {name}（来源 {list(sources)}）；"
        f"在 {target} 补 import 与 __all__ 条目"
    )
    assert name in getattr(sdk_module, "__all__", ()), (
        f"[门面缺符号] {sdk_name}.{name} 未列入 __all__，对插件不可见；"
        f"在 {target} 的 __all__ 补 {name!r}"
    )
    for source_module_name, source_name in sources:
        source_module = importlib.import_module(source_module_name)
        assert hasattr(source_module, source_name), (
            f"{source_module_name} 不再提供 {source_name}"
        )
        assert getattr(sdk_module, name) is getattr(source_module, source_name), (
            f"[门面串对象] {sdk_name}.{name} 与 "
            f"{source_module_name}.{source_name} 不是同一个对象"
        )


def test_binary_module_extra_symbols_stay_out_of_requirements():
    """二进制扩展模块的导出要求取自随仓库提交的 ``.pyi``，不随构建产物增减。"""
    probe = ModuleType(BINARY_MODULE)
    probe.__spec__ = importlib.util.spec_from_loader(BINARY_MODULE, None)
    for name in ("SitesHelper", "SiteSingleton", "SiteRateLimiter"):
        setattr(probe, name, type(name, (), {"__module__": BINARY_MODULE}))
    original = sys.modules[BINARY_MODULE]
    sys.modules[BINARY_MODULE] = probe
    try:
        observed = GENERATOR.collect_required_exports()
    finally:
        sys.modules[BINARY_MODULE] = original

    assert observed == LIVE_REQUIRED_EXPORTS, (
        f"{BINARY_MODULE} 的运行期符号影响了导出要求推导，"
        "生成清单会随构建产物是否就位而漂移"
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
        source
        for _, sources in LIVE_EXPORTS
        for source in sources
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
