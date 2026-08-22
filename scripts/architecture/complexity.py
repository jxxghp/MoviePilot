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


def main() -> int:
    """执行复杂度 baseline check 或显式 write。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前超限基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
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
