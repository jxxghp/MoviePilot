#!/usr/bin/env python3
"""生成并校验 ``app.sdk`` 必须提供的兼容清单投影。"""

import argparse
import ast
import difflib
import importlib
import sys
import types
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
OUTPUT_PATH = PROJECT_ROOT / "app" / "sdk" / "_exports.py"
SDK_PREFIX = "app.sdk"

# 宿主自用的装配与生命周期入口：它们与旧路径同处一个 canonical 模块，
# 但作用是替换宿主提供者、释放宿主资源或回溯宿主调用栈，不构成插件可依赖的接口。
# 另有两个符号的取用位置已由清单其它条目指向 canonical 模块，同样不进入 SDK。
HOST_INTERNAL_EXPORTS = {
    "app.adapters.network.http.aclose_shared_async_transports":
        "关闭宿主共享异步连接池，属应用关停流程",
    "app.adapters.network.http.configure_default_user_agent":
        "设置宿主全局默认 User-Agent",
    "app.adapters.network.http.get_caller":
        "回溯宿主调用栈以标注请求来源",
    "app.domain.context.configure_tmdb_image_url_builder":
        "装配宿主 TMDB 图片地址构造器",
    "app.domain.meta.customization.configure_customization_provider":
        "装配宿主自定义识别词提供者",
    "app.domain.meta.releasegroup.configure_release_groups_provider":
        "装配宿主自定义制作组提供者",
    "app.domain.meta.words.configure_custom_words_provider":
        "装配宿主自定义占位词提供者",
    "app.domain.metainfo.clear_rust_parse_options_cache":
        "重置宿主解析加速器的选项缓存",
    "app.domain.scraper.MediaScraperHelper":
        "刮削助手的取用位置由 app.helper.scraper 条目指向 app.domain.scraper",
    "app.foundation.text.convert":
        "简繁转换的取用位置由 app.utils.zhconv 条目指向 app.foundation.text",
    "app.runtime.extensions.plugin_manager.configure_plugin_catalog_factory":
        "装配宿主插件目录工厂",
    "app.runtime.extensions.plugin_manager.configure_plugin_install_reporter":
        "装配宿主插件安装上报器",
    "app.runtime.extensions.plugin_manager.configure_plugin_legacy_import_services":
        "装配宿主旧导入诊断服务",
    "app.runtime.extensions.plugin_manager.configure_plugin_resource_import_preparer":
        "装配宿主插件资源导入准备器",
    "app.runtime.extensions.plugin_manager.configure_site_auth_level_provider":
        "装配宿主站点认证级别提供者",
}


def stub_surface(module_name: str) -> list[str] | None:
    """
    返回随仓库提交的 ``.pyi`` 声明的公开符号名。

    二进制扩展模块由独立仓库拉取，运行期是真实构建产物还是测试垫片取决于环境，
    其 ``vars()`` 不构成可复现的事实；``.pyi`` 是该模块宿主接口的版本化声明。

    :param module_name: canonical 模块全名
    :return: ``.pyi`` 声明的公开符号名；无 ``.pyi`` 时返回 ``None``
    """
    stub_path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".pyi")
    if not stub_path.is_file():
        return None
    tree = ast.parse(stub_path.read_text(encoding="utf-8"), filename=str(stub_path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            return sorted(
                element.value
                for element in getattr(node.value, "elts", ())
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
                and not element.value.startswith("_")
            )
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    )


def public_surface(module: types.ModuleType) -> list[str]:
    """
    返回模块对外承诺的公开符号名。

    :param module: canonical 模块对象
    :return: ``.pyi`` 声明、``__all__`` 或本模块定义的公开类与函数，按此优先级取用
    """
    declared = stub_surface(module.__name__)
    if declared is not None:
        return declared
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(name for name in declared if not name.startswith("_"))
    return sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and isinstance(value, (type, types.FunctionType))
        and getattr(value, "__module__", None) == module.__name__
    )


def split_replacement(replacement: str) -> tuple[str, str]:
    """
    把清单的 replacement 文案拆成 SDK 模块名与符号名。

    :param replacement: 清单声明的建议取用路径
    :return: ``(SDK 模块名, 符号名)``；模块形态的符号名为空串
    """
    try:
        importlib.import_module(replacement)
    except ImportError:
        module_name, _, symbol_name = replacement.rpartition(".")
        return module_name, symbol_name
    return replacement, ""


def collect_module_requirements(required: dict[str, dict[str, set]]) -> None:
    """
    按模块别名收集 SDK 必须提供的符号及其 canonical 来源。

    :param required: 累积的 ``{SDK 模块: {符号: {(来源模块, 来源符号)}}}`` 映射
    """
    from app.runtime.compat.manifest import MODULE_ALIASES, PACKAGE_ALIASES

    for alias in {**MODULE_ALIASES, **PACKAGE_ALIASES}.values():
        if not alias.replacement.startswith(f"{SDK_PREFIX}."):
            continue
        sdk_name, symbol_name = split_replacement(alias.replacement)
        target = importlib.import_module(alias.target)
        names = [symbol_name] if symbol_name else public_surface(target)
        for name in names:
            if f"{alias.target}.{name}" in HOST_INTERNAL_EXPORTS:
                continue
            required.setdefault(sdk_name, {}).setdefault(name, set()).add(
                (alias.target, name)
            )


def collect_symbol_requirements(required: dict[str, dict[str, set]]) -> None:
    """
    按符号别名收集 SDK 必须提供的符号及其 canonical 来源。

    :param required: 累积的 ``{SDK 模块: {符号: {(来源模块, 来源符号)}}}`` 映射
    """
    from app.runtime.compat.manifest import PACKAGE_EXPORTS, SYMBOL_ALIASES

    for exports in (*PACKAGE_EXPORTS.values(), *SYMBOL_ALIASES.values()):
        for symbol in exports.values():
            if not symbol.replacement.startswith(f"{SDK_PREFIX}."):
                continue
            sdk_name, symbol_name = split_replacement(symbol.replacement)
            if not symbol_name:
                continue
            required.setdefault(sdk_name, {}).setdefault(symbol_name, set()).add(
                (symbol.target_module, symbol.target_name)
            )


def collect_required_exports() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """
    汇总兼容清单对 SDK 提出的全部导出要求。

    :return: ``{SDK 模块: {符号: [(来源模块, 来源符号), ...]}}``
    """
    required: dict[str, dict[str, set]] = {}
    collect_module_requirements(required)
    collect_symbol_requirements(required)
    return {
        sdk_name: {
            name: sorted(sources)
            for name, sources in sorted(symbols.items())
        }
        for sdk_name, symbols in sorted(required.items())
    }


def render_manifest() -> str:
    """
    把导出要求与宿主自用符号渲染为稳定、可审查的 Python 模块。

    :return: 生成文件的完整文本
    """
    required = collect_required_exports()
    lines = [
        '"""由 scripts/sdk/exports.py 生成，请勿手工编辑。"""',
        "",
        "SDK_REQUIRED_EXPORTS = {",
    ]
    for sdk_name, symbols in required.items():
        lines.append(f"    {sdk_name!r}: {{")
        lines.extend(
            f"        {name!r}: {sources!r},"
            for name, sources in symbols.items()
        )
        lines.append("    },")
    lines.extend(["}", "", "SDK_HOST_INTERNAL_EXPORTS = {"])
    lines.extend(
        f"    {path!r}: {reason!r},"
        for path, reason in sorted(HOST_INTERNAL_EXPORTS.items())
    )
    lines.extend(["}", ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """
    解析写入或校验动作。

    :return: 命令行参数
    """
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    """
    写入投影清单，或检查兼容清单对 SDK 的要求是否发生漂移。

    :return: 进程退出码
    """
    from app.testing.bootstrap import prepare_backend

    prepare_backend()
    args = parse_args()
    rendered = render_manifest()
    if args.write:
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        print(f"已写入 {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
        return 0
    current = OUTPUT_PATH.read_text(encoding="utf-8")
    if current == rendered:
        return 0
    print("[清单陈旧] 源码导出要求与 app/sdk/_exports.py 不一致：")
    for line in difflib.unified_diff(
        current.splitlines(),
        rendered.splitlines(),
        fromfile="app/sdk/_exports.py",
        tofile="源码推导结果",
        lineterm="",
    ):
        print(line)
    print("修复：运行 python scripts/sdk/exports.py --write")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
