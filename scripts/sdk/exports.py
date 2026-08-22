#!/usr/bin/env python3
"""生成并校验 ``app.sdk`` 对外承诺的公开面快照。

快照有两个来源，各自成表：

- **别名推导**：兼容清单里 replacement 指向 ``app.sdk.X`` 的旧路径，倒推出 SDK 必须
  提供哪些符号。它天然只覆盖有旧路径别名的符号。
- **门面自报**：SDK 各门面模块 ``__all__`` 里的条目及其 import 来源。声明类这种没有
  任何旧路径别名的新出口只出现在这一表里，否则永远进不了快照。

两表并列而不合并：一个符号缺席时要能分清是「旧路径承诺了而 SDK 没给」还是「SDK 自己
的出口改了来源」，合成一张表就分不出来了。

第三张表记 ``_PluginBase`` 的公开面。它是扩展基类，混着冻结契约、扩展点与内部实现三层，
原样导出而不逐层挑拣；快照的作用是让这三层的增删都成为一次显式改动，而不是随基类改动
悄悄漂移。
"""

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


def facade_modules() -> list[Path]:
    """
    列出插件可见的 SDK 门面模块源码文件。

    :return: ``app/sdk`` 下非下划线开头的模块路径，按名称排序
    """
    return sorted(
        path
        for path in (PROJECT_ROOT / "app" / "sdk").glob("*.py")
        if not path.stem.startswith("_")
    )


def declared_names(tree: ast.Module) -> list[str]:
    """
    读取模块 ``__all__`` 声明的公开符号名。

    :param tree: 模块语法树
    :return: ``__all__`` 中的字符串常量；未声明 ``__all__`` 时为空列表
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        return [
            element.value
            for element in getattr(node.value, "elts", ())
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    return []


def import_origins(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """
    建立模块内绑定名到其 import 来源的映射。

    :param tree: 模块语法树
    :return: ``{本模块绑定名: (来源模块, 来源符号)}``，只含绝对 import
    """
    origins: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            origins[alias.asname or alias.name] = (node.module, alias.name)
    return origins


def collect_declared_exports() -> dict[str, dict[str, tuple[str, str]]]:
    """
    汇总 SDK 各门面模块自报的导出及其来源。

    声明类这类没有旧路径别名的新出口只能由本表覆盖——别名推导按 replacement 文案倒推，
    没有别名就没有条目。本表按源码而不是按运行期属性推导，与 ``.pyi`` 同理：门面导出
    什么是版本化的承诺，不该随构建产物或导入顺序漂移。

    判据取 ``__all__`` 而不是全部顶层 import：``__all__`` 是门面对外承诺的那一份，
    顶层 import 里还有只供本模块内部使用的绑定。架构基线的 ``sdk_exports`` 收的是后者，
    答的是「公开运行契约变没变」这个更宽的问题，两表判据不同因而不合并。

    :return: ``{SDK 模块: {符号: (来源模块, 来源符号)}}``；模块内定义的符号来源即自身
    """
    declared: dict[str, dict[str, tuple[str, str]]] = {}
    for path in facade_modules():
        sdk_name = f"{SDK_PREFIX}.{path.stem}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        origins = import_origins(tree)
        exported = {
            name: origins.get(name, (sdk_name, name))
            for name in sorted(declared_names(tree))
        }
        if exported:
            declared[sdk_name] = exported
    return dict(sorted(declared.items()))


def collect_plugin_base_surface() -> dict[str, list[str]]:
    """
    快照扩展基类 ``_PluginBase`` 对扩展可见的公开面。

    类成员与实例属性分列：前者是扩展覆写或调用的钩子与类属性，后者是基类在
    ``__init__`` 里塞给扩展的宿主门面，两者的增删性质不同，混成一张表就分不出
    「多了个钩子」和「多注入了一个宿主对象」。

    :return: ``{"members": [类成员名], "attributes": [实例属性名]}``
    """
    from app.sdk.extension import _PluginBase

    source = PROJECT_ROOT / "app" / "sdk" / "extension.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    class_body = next(
        node.body
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == _PluginBase.__name__
    )
    attributes: set[str] = set()
    for node in class_body:
        if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
            continue
        for statement in ast.walk(node):
            targets = []
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            attributes.update(
                target.attr
                for target in targets
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and not target.attr.startswith("_")
            )
    return {
        "members": sorted(
            name for name in vars(_PluginBase) if not name.startswith("_")
        ),
        "attributes": sorted(attributes),
    }


def render_manifest() -> str:
    """
    把两个来源的导出快照、宿主自用符号与扩展基类公开面渲染为可审查的 Python 模块。

    :return: 生成文件的完整文本
    """
    lines = [
        '"""由 scripts/sdk/exports.py 生成，请勿手工编辑。"""',
        "",
        "SDK_REQUIRED_EXPORTS = {",
    ]
    for sdk_name, symbols in collect_required_exports().items():
        lines.append(f"    {sdk_name!r}: {{")
        lines.extend(
            f"        {name!r}: {sources!r},"
            for name, sources in symbols.items()
        )
        lines.append("    },")
    lines.extend(["}", "", "SDK_DECLARED_EXPORTS = {"])
    for sdk_name, symbols in collect_declared_exports().items():
        lines.append(f"    {sdk_name!r}: {{")
        lines.extend(
            f"        {name!r}: {source!r},"
            for name, source in symbols.items()
        )
        lines.append("    },")
    lines.extend(["}", "", "SDK_HOST_INTERNAL_EXPORTS = {"])
    lines.extend(
        f"    {path!r}: {reason!r},"
        for path, reason in sorted(HOST_INTERNAL_EXPORTS.items())
    )
    lines.extend(["}", "", "SDK_PLUGIN_BASE_SURFACE = {"])
    for kind, names in sorted(collect_plugin_base_surface().items()):
        lines.append(f"    {kind!r}: [")
        lines.extend(f"        {name!r}," for name in names)
        lines.append("    ],")
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
