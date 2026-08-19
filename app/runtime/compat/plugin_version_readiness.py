"""静态扫描插件源码，报告其对按版本分目录布局（app/plugins/<pid>/<版本号>/）的适配情况。

三类判据：
1. 自引用绝对 import——插件写 ``from app.plugins.<自身pid>.xxx import X`` 引用自己包内的模块，
   版本化后真实路径变成 ``app.plugins.<pid>.<版本目录>.xxx``，该写法会 ``ModuleNotFoundError``。
   宿主不做兼容，插件必须改为相对 import。
2. 跨插件依赖——插件引用其它插件的模块，多版本下同样脆弱，但不是本插件自身的写法错误。
3. 共享声明基类建模——插件在宿主 ``app.db.Base``／``app.db.base.Base`` 上定义模型类，
   同一插件的两个版本会映射到同名表，第二个版本 import 时直接冲突。

本模块只做只读静态分析，不改变插件加载行为。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from app.runtime.compat.resource_imports import _constant_dynamic_import, _dynamic_import_aliases

# 插件可能用来引用宿主共享声明基类 Base 的模块路径。
_SHARED_BASE_MODULES = frozenset({"app.db", "app.db.base"})


@dataclass(frozen=True, slots=True)
class SelfReferentialImportHit:
    """一次自引用绝对 import 命中。"""

    file: str  # 相对插件目录的文件路径
    line: int  # 源码行号
    statement: str  # 原始导入语句原文
    suggestion: str  # 建议改写成的相对 import 写法


@dataclass(frozen=True, slots=True)
class CrossPluginImportHit:
    """一次跨插件 import 命中。"""

    file: str
    line: int
    statement: str
    target_plugin_id: str  # 被依赖插件的目录名


@dataclass(frozen=True, slots=True)
class SharedBaseModelHit:
    """一次继承宿主共享声明基类的模型类定义命中。"""

    file: str
    line: int
    class_name: str


@dataclass(frozen=True, slots=True)
class PluginVersionReadiness:
    """单个插件的多版本目录布局静态扫描结论。"""

    plugin_id: str
    self_referential_imports: tuple[SelfReferentialImportHit, ...] = field(default_factory=tuple)
    cross_plugin_imports: tuple[CrossPluginImportHit, ...] = field(default_factory=tuple)
    shared_base_models: tuple[SharedBaseModelHit, ...] = field(default_factory=tuple)
    unparsed_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_self_referential_imports(self) -> bool:
        return bool(self.self_referential_imports)

    @property
    def has_cross_plugin_imports(self) -> bool:
        return bool(self.cross_plugin_imports)

    @property
    def has_shared_base_models(self) -> bool:
        return bool(self.shared_base_models)

    @property
    def is_clean(self) -> bool:
        """三类判据均未命中且全部文件可解析。"""
        return not (
            self.self_referential_imports
            or self.cross_plugin_imports
            or self.shared_base_models
            or self.unparsed_files
        )


def _classify_plugin_module(module_name: str, own_plugin_id: str) -> tuple[str, str] | None:
    """判断 module_name 是否指向某个插件包，返回 (分类, 目标插件目录名)。

    分类为 "self" 表示指向 own_plugin_id 自身，"cross" 表示指向其它插件；
    module_name 不属于 app.plugins.<pid> 形态时返回 None。
    """
    parts = module_name.split(".")
    if len(parts) < 3 or parts[0] != "app" or parts[1] != "plugins" or not parts[2]:
        return None
    target_plugin_id = parts[2]
    category = "self" if target_plugin_id.lower() == own_plugin_id.lower() else "cross"
    return category, target_plugin_id


def _relative_module_reference(from_package_parts: list[str], target_parts: list[str]) -> str:
    """计算从 from_package_parts 所在包引用 target_parts 对应模块的相对写法（不含 from/import 关键字）。

    :param from_package_parts: 发起 import 的文件所在包，相对插件根目录的目录分段
    :param target_parts: 目标模块相对插件根目录的路径分段（已剥离 app.plugins.<pid> 前缀）
    :return: 形如 "." "..utils" ".sub.utils" 的相对模块引用
    """
    common = 0
    limit = min(len(from_package_parts), len(target_parts))
    while common < limit and from_package_parts[common] == target_parts[common]:
        common += 1
    dots = "." * (len(from_package_parts) - common + 1)
    suffix = ".".join(target_parts[common:])
    return f"{dots}{suffix}" if suffix else dots


def _import_from_suggestion(names: list[ast.alias], relative_ref: str) -> str:
    """拼装 from-import 建议文本。"""
    rendered = ", ".join(
        f"{alias.name} as {alias.asname}" if alias.asname else alias.name
        for alias in names
    )
    return f"from {relative_ref} import {rendered}"


def _import_statement_suggestion(
    alias: ast.alias,
    from_package_parts: list[str],
    target_after_pid: list[str],
) -> str:
    """为 ``import app.plugins.<pid>.xxx`` 形态生成建议改写文案。"""
    if not target_after_pid:
        return (
            "避免用 import 以绝对路径导入自身插件包；"
            "如需引用包内符号，改写为 from . import <符号名>。"
        )
    module_path_parts = target_after_pid[:-1]
    leaf_name = target_after_pid[-1]
    relative_ref = _relative_module_reference(from_package_parts, module_path_parts)
    if alias.asname:
        return f"from {relative_ref} import {leaf_name} as {alias.asname}"
    return (
        f"from {relative_ref} import {leaf_name}；"
        f"并将文件内 {alias.name} 的属性访问改写为 {leaf_name}"
    )


def _dotted_attribute_name(expr: ast.expr) -> str | None:
    """把 Name/Attribute 链还原成点分字符串，其余表达式返回 None。"""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted_attribute_name(expr.value)
        return f"{base}.{expr.attr}" if base else None
    return None


def _collect_base_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """收集文件内可能指向宿主共享 Base 的符号别名与模块别名。

    :return: (符号别名集合, 模块别名集合)。符号别名来自
        ``from app.db[.base] import Base [as X]``；模块别名来自
        ``import app.db[.base] [as X]``（未显式 as 时按 Python 语义绑定为 "app"）。
    """
    symbol_aliases: set[str] = set()
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in _SHARED_BASE_MODULES
            and node.level == 0
        ):
            for alias in node.names:
                if alias.name == "Base":
                    symbol_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SHARED_BASE_MODULES:
                    module_aliases.add(alias.asname or alias.name.split(".")[0])
    return symbol_aliases, module_aliases


def _is_shared_base_reference(
    expr: ast.expr,
    symbol_aliases: set[str],
    module_aliases: set[str],
) -> bool:
    """判断一个基类表达式是否指向宿主共享声明基类 Base。"""
    if isinstance(expr, ast.Name):
        return expr.id in symbol_aliases
    if isinstance(expr, ast.Attribute) and expr.attr == "Base":
        dotted = _dotted_attribute_name(expr.value)
        if dotted is None:
            return False
        return dotted in module_aliases or dotted in _SHARED_BASE_MODULES
    return False


def scan_plugin_version_readiness(plugin_id: str, plugin_dir: Path) -> PluginVersionReadiness:
    """扫描单个插件源码目录，返回其多版本目录布局适配结论。

    :param plugin_id: 插件目录名（即版本化后 app/plugins/<plugin_id>/<版本号>/ 的 <plugin_id>）
    :param plugin_dir: 插件源码目录
    :return: 结构化的静态扫描结论，语法错误等无法解析的文件记录在 unparsed_files 中，不中断扫描
    """
    self_hits: list[SelfReferentialImportHit] = []
    cross_hits: list[CrossPluginImportHit] = []
    base_hits: list[SharedBaseModelHit] = []
    unparsed: list[str] = []

    if not plugin_dir.is_dir():
        return PluginVersionReadiness(plugin_id=plugin_id)

    for path in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative_path = path.relative_to(plugin_dir)
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError, ValueError):
            unparsed.append(str(relative_path))
            continue

        from_package_parts = list(relative_path.parts[:-1])
        importlib_aliases, import_module_aliases = _dynamic_import_aliases(tree)
        symbol_aliases, module_aliases = _collect_base_bindings(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                classification = _classify_plugin_module(node.module, plugin_id)
                if not classification:
                    continue
                category, target_plugin_id = classification
                statement = ast.get_source_segment(source, node) or node.module
                if category == "self":
                    target_suffix = node.module.split(".")[3:]
                    relative_ref = _relative_module_reference(from_package_parts, target_suffix)
                    self_hits.append(
                        SelfReferentialImportHit(
                            file=str(relative_path),
                            line=node.lineno,
                            statement=statement,
                            suggestion=_import_from_suggestion(node.names, relative_ref),
                        )
                    )
                else:
                    cross_hits.append(
                        CrossPluginImportHit(
                            file=str(relative_path),
                            line=node.lineno,
                            statement=statement,
                            target_plugin_id=target_plugin_id,
                        )
                    )
            elif isinstance(node, ast.Import):
                statement = ast.get_source_segment(source, node)
                for alias in node.names:
                    classification = _classify_plugin_module(alias.name, plugin_id)
                    if not classification:
                        continue
                    category, target_plugin_id = classification
                    if category == "self":
                        target_after_pid = alias.name.split(".")[3:]
                        self_hits.append(
                            SelfReferentialImportHit(
                                file=str(relative_path),
                                line=node.lineno,
                                statement=statement or alias.name,
                                suggestion=_import_statement_suggestion(
                                    alias, from_package_parts, target_after_pid,
                                ),
                            )
                        )
                    else:
                        cross_hits.append(
                            CrossPluginImportHit(
                                file=str(relative_path),
                                line=node.lineno,
                                statement=statement or alias.name,
                                target_plugin_id=target_plugin_id,
                            )
                        )
            elif isinstance(node, ast.Call):
                module_name = _constant_dynamic_import(
                    node,
                    importlib_aliases=importlib_aliases,
                    import_module_aliases=import_module_aliases,
                )
                if not module_name:
                    continue
                classification = _classify_plugin_module(module_name, plugin_id)
                if not classification:
                    continue
                category, target_plugin_id = classification
                statement = ast.get_source_segment(source, node) or module_name
                if category == "self":
                    target_suffix = module_name.split(".")[3:]
                    if target_suffix:
                        relative_ref = _relative_module_reference(from_package_parts, target_suffix)
                        suggestion = (
                            f"改为静态相对 import：from {relative_ref} import <所需符号>"
                            "（importlib.import_module 的绝对字符串路径在版本化目录下会失效）"
                        )
                    else:
                        suggestion = (
                            "避免用 importlib.import_module 以绝对路径导入自身插件包；"
                            "改为静态相对 import（如 from . import <符号名>）。"
                        )
                    self_hits.append(
                        SelfReferentialImportHit(
                            file=str(relative_path),
                            line=node.lineno,
                            statement=statement,
                            suggestion=suggestion,
                        )
                    )
                else:
                    cross_hits.append(
                        CrossPluginImportHit(
                            file=str(relative_path),
                            line=node.lineno,
                            statement=statement,
                            target_plugin_id=target_plugin_id,
                        )
                    )
            elif isinstance(node, ast.ClassDef):
                if any(
                    _is_shared_base_reference(base, symbol_aliases, module_aliases)
                    for base in node.bases
                ):
                    base_hits.append(
                        SharedBaseModelHit(
                            file=str(relative_path),
                            line=node.lineno,
                            class_name=node.name,
                        )
                    )

    return PluginVersionReadiness(
        plugin_id=plugin_id,
        self_referential_imports=tuple(self_hits),
        cross_plugin_imports=tuple(cross_hits),
        shared_base_models=tuple(base_hits),
        unparsed_files=tuple(unparsed),
    )
