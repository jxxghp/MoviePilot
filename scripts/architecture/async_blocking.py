"""检测关键 async 路径中新引入的直接阻塞调用。"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/async-blocking-baseline.json"
SCAN_ROOTS = ("app/api", "app/agent", "app/application")
BLOCKING_EXACT = {
    "open",
    "time.sleep",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.run",
    "os.listdir",
    "os.scandir",
    "os.walk",
    "requests.delete",
    "requests.get",
    "requests.head",
    "requests.patch",
    "requests.post",
    "requests.put",
    "requests.request",
}
BLOCKING_ATTRIBUTES = {
    "glob",
    "iterdir",
    "open",
    "read_bytes",
    "read_text",
    "rglob",
    "write_bytes",
    "write_text",
}


def _call_name(node: ast.Call) -> str:
    """把简单名称和属性调用还原为点分文本。"""
    parts = []
    target: ast.expr = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _root_name(expression: ast.expr) -> str | None:
    """返回属性调用最左侧的变量名。"""
    while isinstance(expression, ast.Attribute):
        expression = expression.value
    return expression.id if isinstance(expression, ast.Name) else None


class _AsyncPathCollector(ast.NodeVisitor):
    """用局部数据流识别 anyio AsyncPath 变量及其派生值。"""

    def __init__(self, function: ast.AsyncFunctionDef) -> None:
        """从参数注解初始化 AsyncPath 变量集合。"""
        self.paths = {
            argument.arg
            for argument in (*function.args.posonlyargs, *function.args.args)
            if argument.annotation and "AsyncPath" in ast.unparse(argument.annotation)
        }
        self.path_collections: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> None:
        """识别 AsyncPath 构造和已知路径的 `/` 派生赋值。"""
        is_async_path = (
            isinstance(node.value, ast.Call)
            and _call_name(node.value).endswith("AsyncPath")
        ) or (
            isinstance(node.value, ast.BinOp)
            and isinstance(node.value.left, ast.Name)
            and node.value.left.id in self.paths
        )
        if is_async_path:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.paths.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """识别 `list[AsyncPath]` 等路径集合。"""
        if isinstance(node.target, ast.Name):
            annotation = ast.unparse(node.annotation)
            if "AsyncPath" in annotation:
                if "list" in annotation or "List" in annotation:
                    self.path_collections.add(node.target.id)
                else:
                    self.paths.add(node.target.id)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """AsyncPath.iterdir 产出的元素仍是 AsyncPath。"""
        if isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Call):
            receiver = _root_name(node.iter.func)
            if receiver in self.paths:
                self.paths.add(node.target.id)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        """从 `list[AsyncPath]` 迭代得到的元素仍是 AsyncPath。"""
        if (
            isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Name)
            and node.iter.id in self.path_collections
        ):
            self.paths.add(node.target.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """不分析嵌套同步函数。"""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """不分析嵌套异步函数。"""


class _AsyncCallVisitor(ast.NodeVisitor):
    """只收集一个 async 函数本体中的阻塞调用，不进入嵌套函数定义。"""

    def __init__(self, async_paths: set[str]) -> None:
        """初始化违规计数器和异步文件对象白名单。"""
        self.calls: Counter[str] = Counter()
        self._async_paths = async_paths

    def visit_Call(self, node: ast.Call) -> None:
        """记录命中阻塞名单的调用并继续遍历参数表达式。"""
        name = _call_name(node)
        attribute = name.rsplit(".", 1)[-1]
        receiver = _root_name(node.func)
        async_safe = name.startswith("aiofiles.") or receiver in self._async_paths
        if not async_safe and (
            name in BLOCKING_EXACT or attribute in BLOCKING_ATTRIBUTES
        ):
            self.calls[name or attribute] += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """嵌套同步函数不属于外层 async 的直接执行体。"""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """嵌套异步函数由模块级收集器单独治理。"""


def _async_functions(tree: ast.Module):
    """产出模块顶层及类直接拥有的 async 函数限定名。"""
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.AsyncFunctionDef):
                    yield f"{node.name}.{method.name}", method


def collect_async_blocking(root: Path = PROJECT_ROOT) -> dict[str, int]:
    """扫描关键目录并以文件、函数、调用名聚合存量次数。"""
    debt: Counter[str] = Counter()
    for scan_root in SCAN_ROOTS:
        for path in sorted((root / scan_root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(root).as_posix()
            for qualname, function in _async_functions(tree):
                collector = _AsyncPathCollector(function)
                for statement in function.body:
                    collector.visit(statement)
                visitor = _AsyncCallVisitor(collector.paths)
                for statement in function.body:
                    visitor.visit(statement)
                for call_name, count in visitor.calls.items():
                    debt[f"{relative}:{qualname}:{call_name}"] += count
    return dict(sorted(debt.items()))


def compare_async_blocking(
    baseline: dict[str, int], current: dict[str, int]
) -> list[str]:
    """允许存量减少或删除，拒绝新增阻塞调用和调用次数增长。"""
    problems = []
    for entry, count in current.items():
        previous = baseline.get(entry)
        if previous is None:
            problems.append(f"新增 async 阻塞调用：{entry} x{count}")
        elif count > previous:
            problems.append(f"async 阻塞调用增长：{entry} x{count}>{previous}")
    return problems


def main() -> int:
    """执行 async 阻塞 baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前阻塞债务")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = collect_async_blocking()
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.baseline.relative_to(PROJECT_ROOT)}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = compare_async_blocking(baseline, current)
    if problems:
        print("\n".join(problems))
        return 1
    print("async 阻塞调用 ratchet 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
