"""函数级重复代码门禁。

对非插件模块做归一化 AST 指纹比对：两个及以上不同模块中出现同构函数
（变量名、字面量已归一，仅保留结构与属性名），且指纹规模超过阈值时告警。

存量复制粘贴以白名单标注，随各 Phase 清理后同步收紧；新增重复不得越过阈值。
"""
import ast
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"

# 指纹长度阈值：低于该值视为偶然相似，不告警。
MIN_FINGERPRINT_SIZE = 1000
# 参与告警的最小函数体节点数：过滤 setter/getter 等小函数。
MIN_FUNCTION_SIZE = 40
# 存量白名单：(模块名, 函数名) 集合，各 Phase 清理后同步移除。
KNOWN_DUPLICATES: set[tuple[str, str]] = set()


def _normalize(node: ast.AST) -> str:
    """把函数 AST 归一化为指纹字符串。

    只保留控制流骨架与属性访问名，统一变量名与字面量，
    使改名、改常量的复制粘贴仍能被识别为同构。
    """
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.FunctionDef):
            parts.append(f"F:{child.name}")
        elif isinstance(child, ast.Name):
            parts.append(f"V:{child.id}")
        elif isinstance(child, ast.Attribute):
            parts.append(f"A:{child.attr}")
        elif isinstance(child, ast.Constant):
            parts.append("C:lit")
        elif isinstance(child, ast.Call):
            parts.append("call")
        elif isinstance(child, ast.BinOp):
            parts.append(f"op:{type(child.op).__name__}")
        elif isinstance(child, ast.Compare):
            parts.append("cmp")
        elif isinstance(child, ast.UnaryOp):
            parts.append("uop")
        elif isinstance(child, ast.BoolOp):
            parts.append("boolop")
        elif isinstance(child, ast.If):
            parts.append("if")
        elif isinstance(child, ast.For):
            parts.append("for")
        elif isinstance(child, ast.While):
            parts.append("while")
        elif isinstance(child, ast.Try):
            parts.append("try")
        elif isinstance(child, ast.Return):
            parts.append("return")
        elif isinstance(child, ast.Assign):
            parts.append("assign")
        elif isinstance(child, ast.AnnAssign):
            parts.append("annassign")
        elif isinstance(child, ast.AugAssign):
            parts.append("augassign")
        elif isinstance(child, ast.Dict):
            parts.append("dict")
        elif isinstance(child, ast.List):
            parts.append("list")
        elif isinstance(child, ast.Subscript):
            parts.append("sub")
        elif isinstance(child, ast.Lambda):
            parts.append("lambda")
        elif isinstance(child, ast.Expr):
            parts.append("expr")
        elif isinstance(child, ast.With):
            parts.append("with")
        elif isinstance(child, ast.Yield):
            parts.append("yield")
        elif isinstance(child, ast.Import):
            parts.append("import")
        elif isinstance(child, ast.ImportFrom):
            parts.append("importfrom")
        elif isinstance(child, ast.Pass):
            parts.append("pass")
        elif isinstance(child, ast.arguments):
            parts.append("args")
    return "|".join(parts)


def _collect_duplicates() -> dict[int, list[tuple[str, str, int, int]]]:
    """扫描非插件模块，按归一化指纹分组收集跨模块同构函数。"""
    fingerprints: dict[int, list[tuple[str, str, int, int]]] = defaultdict(list)
    for path in APP_ROOT.rglob("*.py"):
        if path.relative_to(APP_ROOT).parts[0] == "plugins":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError:
            continue
        relative = path.relative_to(PROJECT_ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module_name = ".".join(parts)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if sum(1 for _ in ast.walk(node)) < MIN_FUNCTION_SIZE:
                continue
            fingerprint = _normalize(node)
            if len(fingerprint) < MIN_FINGERPRINT_SIZE:
                continue
            fingerprints[hash(fingerprint)].append(
                (module_name, node.name, node.lineno, len(fingerprint))
            )
    return fingerprints


def test_no_new_large_duplicate_functions():
    """跨模块同构大函数（指纹 >= 1000）不得出现，存量白名单除外。"""
    violations: list[list[tuple[str, str, int, int]]] = []
    for items in _collect_duplicates().values():
        modules = {module_name for module_name, _, _, _ in items}
        if len(modules) < 2:
            continue
        leftovers = [
            item for item in items if (item[0], item[1]) not in KNOWN_DUPLICATES
        ]
        if leftovers:
            violations.append(leftovers)
    assert violations == [], (
        "检测到跨模块同构大函数（疑似复制粘贴），请提取公共基类/工具或"
        "先加入 KNOWN_DUPLICATES 白名单并随对应 Phase 清理：\n"
        + "\n".join(
            f"  {module_name}.{func_name} (line {line})"
            for group in violations
            for module_name, func_name, line, _ in group
        )
    )
