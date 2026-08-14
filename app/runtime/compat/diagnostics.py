import ast
import inspect
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    SYMBOL_ALIASES,
    ModuleAlias,
    SymbolAlias,
)


WarningEmitter = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class LegacyImportUsage:
    """记录一次旧导入路径命中及其调用来源。"""

    consumer: str
    legacy_module: str
    origin: Optional[str] = None


_lock = threading.RLock()
_enabled: Optional[bool] = None
_emitter: Optional[WarningEmitter] = None
_pending: List[LegacyImportUsage] = []
_reported: Set[Tuple[str, str]] = set()
_hits: Set[Tuple[str, str]] = set()
_scan_cache: Dict[Path, Tuple[int, int, Tuple[Tuple[str, int], ...], Optional[str]]] = {}


def _find_import_consumer() -> str:
    """从导入调用栈中识别触发兼容导入的插件或主程序模块。"""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame:
            module_name = str(frame.f_globals.get("__name__") or "")
            if module_name and not (
                module_name.startswith("importlib")
                or module_name.startswith("app.runtime.compat")
            ):
                return module_name
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def _find_alias(legacy_path: str) -> Optional[Union[ModuleAlias, SymbolAlias]]:
    """查找旧模块或旧符号对应的精确兼容规则。"""
    module_alias = MODULE_ALIASES.get(legacy_path) or PACKAGE_ALIASES.get(
        legacy_path
    )
    if module_alias:
        return module_alias
    module_name, separator, symbol_name = legacy_path.rpartition(".")
    if not separator:
        return None
    return SYMBOL_ALIASES.get(module_name, {}).get(symbol_name)


def _format_warning(
        usage: LegacyImportUsage,
        alias: Union[ModuleAlias, SymbolAlias],
) -> str:
    """生成包含实际目标和推荐 SDK 路径的单行兼容警告。"""
    consumer = usage.consumer
    if consumer.startswith("app.plugins."):
        parts = consumer.split(".")
        source = f"插件 {parts[2] if len(parts) > 2 else consumer}"
    else:
        source = f"模块 {consumer}"
    origin = f"（{usage.origin}）" if usage.origin else ""
    target = (
        alias.target
        if isinstance(alias, ModuleAlias)
        else f"{alias.target_module}.{alias.target_name}"
    )
    return (
        f"[兼容导入] {source}{origin} 使用旧路径 {usage.legacy_module}，"
        f"已映射到 {target}；请迁移到 {alias.replacement}"
    )


def _emit_usage(usage: LegacyImportUsage) -> None:
    """按调用方和旧路径去重后输出兼容警告。"""
    alias = _find_alias(usage.legacy_module)
    if not alias:
        return
    key = (usage.consumer, usage.legacy_module)
    with _lock:
        _hits.add(key)
        if _enabled is None:
            _pending.append(usage)
            return
        if not _enabled or key in _reported or not _emitter:
            return
        _reported.add(key)
        emitter = _emitter
    emitter(_format_warning(usage, alias))


def record_legacy_import(legacy_module: str) -> None:
    """记录运行时 Finder 实际解析到的旧模块路径。"""
    with _lock:
        if _enabled is False:
            return
    _emit_usage(
        LegacyImportUsage(
            consumer=_find_import_consumer(),
            legacy_module=legacy_module,
        )
    )


def configure_legacy_import_diagnostics(
    *,
    enabled: bool,
    emitter: WarningEmitter,
) -> None:
    """
    配置旧导入诊断，并在 DEBUG 开启时刷新安装早期暂存的命中。

    :param enabled: 是否启用兼容警告
    :param emitter: 接收单行警告文本的日志回调
    """
    with _lock:
        global _enabled, _emitter
        _enabled = enabled
        _emitter = emitter
        pending = list(_pending) if enabled else []
        _pending.clear()
    for usage in pending:
        _emit_usage(usage)


def _extract_legacy_imports(tree: ast.AST) -> Tuple[Tuple[str, int], ...]:
    """从插件 AST 中提取映射表已登记的静态旧导入。"""
    matches: Set[Tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in MODULE_ALIASES or imported.name in PACKAGE_ALIASES:
                    matches.add((imported.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in MODULE_ALIASES or node.module in PACKAGE_ALIASES:
                matches.add((node.module, node.lineno))
            for imported in node.names:
                candidate = f"{node.module}.{imported.name}"
                if (
                    candidate in MODULE_ALIASES
                    or candidate in PACKAGE_ALIASES
                    or imported.name in SYMBOL_ALIASES.get(node.module, {})
                ):
                    matches.add((candidate, node.lineno))
        elif isinstance(node, ast.Call) and node.args:
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                function_name = f"{node.func.value.id}.{node.func.attr}"
            if function_name not in {"__import__", "importlib.import_module"}:
                continue
            argument = node.args[0]
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and (
                    argument.value in MODULE_ALIASES
                    or argument.value in PACKAGE_ALIASES
                )
            ):
                matches.add((argument.value, node.lineno))
    return tuple(sorted(matches, key=lambda item: (item[1], item[0])))


def _scan_plugin_file(path: Path) -> Tuple[Tuple[Tuple[str, int], ...], Optional[str]]:
    """解析单个插件文件，并按文件状态缓存旧导入扫描结果。"""
    stat = path.stat()
    with _lock:
        cached = _scan_cache.get(path)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2], cached[3]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        matches = _extract_legacy_imports(tree)
        error = None
    except (OSError, SyntaxError, UnicodeError) as err:
        matches = ()
        error = str(err)
    with _lock:
        _scan_cache[path] = (stat.st_mtime_ns, stat.st_size, matches, error)
    return matches, error


def scan_plugin_legacy_imports(plugin_id: str, plugin_dir: Path) -> None:
    """
    在 DEBUG 模式下扫描插件源码，补足 sys.modules 缓存导致的 Finder 漏报。

    :param plugin_id: 插件 ID
    :param plugin_dir: 插件源码目录
    """
    with _lock:
        enabled = bool(_enabled)
        emitter = _emitter
    if not enabled or not emitter:
        return
    for path in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        matches, error = _scan_plugin_file(path)
        relative_path = path.relative_to(plugin_dir)
        if error:
            emitter(f"[兼容导入] DEBUG 扫描插件 {plugin_id} 文件 {relative_path} 失败：{error}")
            continue
        for legacy_module, line_number in matches:
            _emit_usage(
                LegacyImportUsage(
                    consumer=f"app.plugins.{plugin_id.lower()}",
                    legacy_module=legacy_module,
                    origin=f"{relative_path}:{line_number}",
                )
            )


def get_legacy_import_diagnostics() -> Dict[str, object]:
    """返回只读诊断快照，供测试和后续 doctor 能力使用。"""
    with _lock:
        return {
            "enabled": _enabled,
            "hits": sorted(_hits),
            "reported": sorted(_reported),
            "pending": len(_pending),
        }


def reset_legacy_import_diagnostics() -> None:
    """清空进程级诊断状态，仅供隔离测试使用。"""
    with _lock:
        global _enabled, _emitter
        _enabled = None
        _emitter = None
        _pending.clear()
        _reported.clear()
        _hits.clear()
        _scan_cache.clear()
