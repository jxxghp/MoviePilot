"""为 API/Application/Chain 公共入口维护只降不增的行数预算。"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/complexity-baseline.json"
V2_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/complexity-v2-baseline.json"


@dataclass(frozen=True, slots=True)
class ComplexityRule:
    """描述目录、入口选择器与最大源代码行数。"""

    name: str
    root: str
    budget: int
    endpoint_only: bool = False


RULES = (
    ComplexityRule("api_endpoint", "app/api/endpoints", 80, endpoint_only=True),
    ComplexityRule("application_public", "app/application", 150),
    ComplexityRule("chain_public", "app/chain", 150),
)


def _iter_python_files(root: Path, roots: tuple[str, ...]) -> Iterable[Path]:
    """遍历 canonical 宿主源码，明确排除运行时插件副本。"""
    for scan_root in roots:
        path = root / scan_root
        if path.is_file() and path.suffix == ".py":
            yield path
            continue
        if not path.is_dir():
            continue
        for candidate in sorted(path.rglob("*.py")):
            relative = candidate.relative_to(root).as_posix()
            if relative == "app/plugins" or relative.startswith("app/plugins/"):
                continue
            yield candidate


def _walk_functions(
    nodes: Iterable[ast.AST],
    prefix: str = "",
) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """递归收集函数、方法和嵌套编排器，避免只检查公开入口。"""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            if not node.name.startswith("__"):
                yield qualname, node
            yield from _walk_functions(node.body, qualname)
        elif isinstance(node, ast.ClassDef):
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            yield from _walk_functions(node.body, qualname)
        elif isinstance(node, ast.If):
            yield from _walk_functions(node.body, prefix)
            yield from _walk_functions(node.orelse, prefix)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            yield from _walk_functions(node.body, prefix)
            yield from _walk_functions(node.orelse, prefix) if isinstance(node, (ast.For, ast.AsyncFor)) else ()
        elif isinstance(node, ast.Try):
            yield from _walk_functions(node.body, prefix)
            yield from _walk_functions(node.orelse, prefix)
            yield from _walk_functions(node.finalbody, prefix)
            for handler in node.handlers:
                yield from _walk_functions(handler.body, prefix)


def _walk_classes(
    nodes: Iterable[ast.AST],
    prefix: str = "",
) -> Iterable[tuple[str, ast.ClassDef]]:
    """递归收集类体长度，覆盖 Scheduler 和私有 owner。"""
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            yield qualname, node
            yield from _walk_classes(node.body, qualname)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _walk_classes(node.body, prefix)


def collect_complexity_v2(root: Path = PROJECT_ROOT) -> dict[str, dict[str, int]]:
    """收集私有方法、类、文件和 Scheduler 的可审计复杂度事实。"""
    roots = ("app/api/endpoints", "app/application", "app/chain", "app/scheduler")
    report: dict[str, dict[str, int]] = {"method": {}, "class": {}, "file": {}}
    for path in _iter_python_files(root, roots):
        relative = path.relative_to(root).as_posix()
        source_lines = path.read_text(encoding="utf-8").splitlines()
        if len(source_lines) > 1000:
            report["file"][relative] = len(source_lines)
        tree = ast.parse("\n".join(source_lines), filename=str(path))
        for qualname, node in _walk_functions(tree.body):
            line_count = (node.end_lineno or node.lineno) - node.lineno + 1
            if line_count > 150:
                report["method"][f"{relative}:{qualname}"] = line_count
        for qualname, node in _walk_classes(tree.body):
            line_count = (node.end_lineno or node.lineno) - node.lineno + 1
            if line_count > 500:
                report["class"][f"{relative}:{qualname}"] = line_count
    return report


def _is_endpoint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """识别带常见 HTTP method decorator 的 API endpoint。"""
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr.lower() in methods:
            return True
    return False


def _public_entries(
    tree: ast.Module, endpoint_only: bool
) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """产出顶层函数和类直接拥有的公共方法，不把嵌套 helper 重复计数。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") and (
                not endpoint_only or _is_endpoint(node)
            ):
                yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not method.name.startswith("_") and (
                        not endpoint_only or _is_endpoint(method)
                    ):
                        yield f"{node.name}.{method.name}", method


def collect_complexity(root: Path = PROJECT_ROOT) -> dict[str, dict[str, int]]:
    """收集每条规则下当前超过预算的入口及其精确源代码行数。"""
    report: dict[str, dict[str, int]] = {}
    for rule in RULES:
        debt: dict[str, int] = {}
        for path in sorted((root / rule.root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(root).as_posix()
            for qualname, node in _public_entries(tree, rule.endpoint_only):
                line_count = (node.end_lineno or node.lineno) - node.lineno + 1
                if line_count > rule.budget:
                    debt[f"{relative}:{qualname}"] = line_count
        report[rule.name] = debt
    return report


def compare_complexity(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[str]:
    """返回新增超限或既有超限增长问题；删除和缩短均合法。"""
    problems = []
    for rule in RULES:
        previous = baseline.get(rule.name, {})
        for entry, line_count in current.get(rule.name, {}).items():
            if entry not in previous:
                problems.append(f"{rule.name}: 新增超限 {entry}={line_count}>{rule.budget}")
            elif line_count > previous[entry]:
                problems.append(
                    f"{rule.name}: 既有超限增长 {entry}={line_count}>{previous[entry]}"
                )
    return problems


def compare_complexity_v2(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[str]:
    """返回 v2 私有方法、类和文件事实的新增或增长问题。"""
    problems: list[str] = []
    for category in baseline.keys() | current.keys():
        previous = baseline.get(category, {})
        for entry, line_count in current.get(category, {}).items():
            if entry not in previous:
                problems.append(f"{category}: 新增超限 {entry}={line_count}")
            elif line_count > previous[entry]:
                problems.append(
                    f"{category}: 既有超限增长 {entry}={line_count}>{previous[entry]}"
                )
    return problems


def main() -> int:
    """执行复杂度 baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前超限基线")
    parser.add_argument("--v2", action="store_true", help="检查私有方法、类、文件和 Scheduler")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    if args.v2:
        baseline_path = args.baseline if args.baseline != DEFAULT_BASELINE else V2_BASELINE
        current_v2 = collect_complexity_v2()
        if args.write:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(current_v2, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"已写入 {baseline_path.relative_to(PROJECT_ROOT)}")
            return 0
        baseline_v2 = json.loads(baseline_path.read_text(encoding="utf-8"))
        problems_v2 = compare_complexity_v2(baseline_v2, current_v2)
        if problems_v2:
            print("\n".join(problems_v2))
            return 1
        print("复杂度 v2 ratchet 通过")
        return 0
    current = collect_complexity()
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.baseline.relative_to(PROJECT_ROOT)}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = compare_complexity(baseline, current)
    if problems:
        print("\n".join(problems))
        return 1
    print("复杂度 ratchet 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
