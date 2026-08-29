"""从旧插件源码导入中识别必须提前就绪的宿主资源。"""

from __future__ import annotations

import ast
import threading
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Set, Tuple


@dataclass(frozen=True, slots=True)
class ResourceImportRule:
    """描述第三方模块导入与宿主资源能力之间的静态映射。"""

    capability_id: str  # 导入前必须准备的宿主能力标识
    module_prefixes: tuple[str, ...]  # 按完整包边界匹配的第三方模块前缀
    headed_entrypoints: tuple[str, ...]  # 已确认允许 headed 模式的公开入口


# 旧插件可能绕过宿主浏览器门面直接调用 CloakBrowser。其六个 launch
# 入口均允许 headed 模式，因此导入该包或任意子模块时保守准备虚拟显示。
RESOURCE_IMPORT_RULES: tuple[ResourceImportRule, ...] = (
    ResourceImportRule(
        capability_id="host.display",
        module_prefixes=("cloakbrowser",),
        headed_entrypoints=(
            "launch",
            "launch_async",
            "launch_context",
            "launch_context_async",
            "launch_persistent_context",
            "launch_persistent_context_async",
        ),
    ),
)


_scan_cache_lock = threading.RLock()
_scan_cache: Dict[Path, Tuple[int, int, int, int, int, FrozenSet[str]]] = {}


class PluginResourceImportScanError(RuntimeError):
    """表示单个插件源码无法生成可靠的精确资源集合。"""


def _all_resource_capabilities() -> FrozenSet[str]:
    """扫描不完整时返回全部已登记资源，避免漏失导入前置条件。"""
    return frozenset(rule.capability_id for rule in RESOURCE_IMPORT_RULES)


def _matches_module(module_name: str, module_prefixes: Iterable[str]) -> bool:
    """按完整包边界匹配模块，避免相似名称产生误报。"""
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in module_prefixes
    )


def _dynamic_import_aliases(tree: ast.AST) -> tuple[Set[str], Set[str]]:
    """收集 importlib 模块及 import_module 函数的本地别名。"""
    module_aliases = {"importlib"}
    function_aliases: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "importlib":
                    module_aliases.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for imported in node.names:
                if imported.name == "import_module":
                    function_aliases.add(imported.asname or imported.name)
    return module_aliases, function_aliases


def _constant_dynamic_import(
    node: ast.Call,
    *,
    importlib_aliases: Set[str],
    import_module_aliases: Set[str],
) -> str | None:
    """提取受支持动态导入调用中的常量模块名。"""
    if not node.args:
        return None
    is_import_call = isinstance(node.func, ast.Name) and (
        node.func.id == "__import__" or node.func.id in import_module_aliases
    )
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in importlib_aliases
        and node.func.attr == "import_module"
    ):
        is_import_call = True
    if not is_import_call:
        return None
    argument = node.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _imported_modules(tree: ast.AST) -> FrozenSet[str]:
    """提取静态导入以及可确定目标的动态导入模块名。"""
    modules: Set[str] = set()
    importlib_aliases, import_module_aliases = _dynamic_import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(imported.name for imported in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Call):
            module_name = _constant_dynamic_import(
                node,
                importlib_aliases=importlib_aliases,
                import_module_aliases=import_module_aliases,
            )
            if module_name:
                modules.add(module_name)
    return frozenset(modules)


def _scan_source(plugin_id: str, path: Path) -> FrozenSet[str]:
    """读取并解析单个源码文件；不完整结果不能进入插件导入阶段。"""
    try:
        before_stat = path.stat()
        # 热加载工具可能保留 mtime，等长替换也不会改变 size；ctime 与 inode/device
        # 一并参与身份判断，避免把已替换源码误认为旧缓存。
        cache_key = (
            before_stat.st_mtime_ns,
            before_stat.st_ctime_ns,
            before_stat.st_size,
            before_stat.st_dev,
            before_stat.st_ino,
        )
        with _scan_cache_lock:
            cached = _scan_cache.get(path)
        if cached and cached[:5] == cache_key:
            return cached[5]
        with tokenize.open(path) as source_file:
            source = source_file.read()
        tree = ast.parse(source, filename=str(path))
        after_stat = path.stat()
    except (OSError, SyntaxError, UnicodeError) as error:
        raise PluginResourceImportScanError(
            f"无法扫描插件 {plugin_id} 源码 {path.name}：{error}"
        ) from error
    after_key = (
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
        after_stat.st_size,
        after_stat.st_dev,
        after_stat.st_ino,
    )
    if cache_key != after_key:
        raise PluginResourceImportScanError(
            f"扫描插件 {plugin_id} 时源码 {path.name} 发生变化"
        )

    capabilities: Set[str] = set()
    for module_name in _imported_modules(tree):
        for rule in RESOURCE_IMPORT_RULES:
            if _matches_module(module_name, rule.module_prefixes):
                capabilities.add(rule.capability_id)
    result = frozenset(capabilities)
    with _scan_cache_lock:
        _scan_cache[path] = (*cache_key, result)
    return result


def scan_plugin_resource_imports(
    plugin_id: str,
    plugin_dir: Path,
) -> tuple[str, ...]:
    """递归扫描插件源码并返回导入前必须准备的 capability ID。"""
    if not plugin_dir.is_dir():
        raise PluginResourceImportScanError(
            f"插件 {plugin_id} 源码目录不存在：{plugin_dir}"
        )

    capabilities: Set[str] = set()
    try:
        source_files = sorted(plugin_dir.rglob("*.py"))
    except OSError:
        return tuple(sorted(_all_resource_capabilities()))
    for path in source_files:
        if "__pycache__" in path.parts:
            continue
        try:
            capabilities.update(_scan_source(plugin_id, path))
        except PluginResourceImportScanError:
            # Python 最终只会导入真实依赖链；无法解析的残留或平台专用文件不应
            # 阻断整个插件，但必须按最保守资源集合准备后再交给 loader 判断。
            capabilities.update(_all_resource_capabilities())
    return tuple(sorted(capabilities))
