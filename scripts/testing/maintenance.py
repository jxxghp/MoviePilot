"""
只读清点测试归属、跨测试导入和重复候选；禁止平铺测试文件及跨测试依赖。

相同函数体只能作为人工审查候选：模块配置、夹具、入口和前置状态仍可能不同。
本工具不删除文件、不修改基线，也不以测试数量或行数决定通过与否。
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_imports(source: str, path: str) -> set[str]:
    """
    提取静态导入的测试模块，包含相对导入及从包内导入测试模块的形式。

    :param source: Python 源码
    :param path: 仓库相对文件路径
    :return: 被导入的测试模块名称集合
    """
    modules = set()
    package = ".".join(Path(path).parent.parts)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            modules.add(module)
            if not any(part.startswith("test_") for part in module.split(".")):
                modules.update(f"{module}.{alias.name}" for alias in node.names)
    return {
        module for module in modules
        if (module.startswith("tests.") or module.startswith("test_"))
        and any(part.startswith("test_") for part in module.split("."))
    }


def inventory(root: Path) -> dict:
    """
    扫描当前工作树中的测试文件，生成可归档的维护报告。

    :param root: 后端仓库根目录
    :return: 文件和函数计数、责任域分布、导入边及相同函数体候选
    """
    owners: Counter[str] = Counter()
    bodies: dict[str, list[str]] = defaultdict(list)
    imports = []
    files = []
    definitions = 0
    for path in sorted((root / "tests").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports.extend([relative, module] for module in sorted(test_imports(source, relative)))
        if not path.name.startswith("test_"):
            continue
        files.append({"path": relative, "lines": len(source.splitlines())})
        owners[path.parent.relative_to(root / "tests").as_posix()] += 1
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            definitions += 1
            candidate = copy.deepcopy(node)
            candidate.name = "test_candidate"
            if ast.get_docstring(candidate):
                candidate.body = candidate.body[1:]
            bodies[ast.dump(candidate, include_attributes=False)].append(f"{relative}::{node.name}")
    return {
        "files": len(files),
        "definitions": definitions,
        "lines": sum(item["lines"] for item in files),
        "owners": dict(sorted(owners.items())),
        "test_module_imports": imports,
        "duplicate_body_candidates": [group for group in bodies.values() if len(group) > 1],
        "largest_files": sorted(files, key=lambda item: (-item["lines"], item["path"]))[:10],
    }


def layout_violations(root: Path, report: dict) -> list[str]:
    """
    检查完整测试树，不因基准提交中曾有相同旧路径或导入而放行。

    :param root: 后端仓库根目录
    :param report: 当前工作树的清点结果
    :return: 目录归属和共享设施违规的可读说明
    """
    violations = [
        f"根目录测试应归入责任域：{path.relative_to(root).as_posix()}"
        for path in sorted((root / "tests").glob("test_*.py"))
    ]
    violations.extend(
        f"共享设施应移入 support 或局部 conftest：{path} -> {module}"
        for path, module in report["test_module_imports"]
    )
    return violations


def compare_base(root: Path, base: str) -> dict:
    """
    生成测试文件增删清单，供审查迁移、合并和退役，不改变全量检查结果。

    :param root: 后端仓库根目录
    :param base: 比较基准提交或引用
    :return: 已解析基准以及相对基准的文件增删摘要
    """
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=root, text=True,
    ).strip()
    previous = set(subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", revision, "--", "tests"], cwd=root, text=True,
    ).splitlines())
    current_tests = {
        path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py")
    }
    previous_tests = {path for path in previous if Path(path).match("test_*.py")}
    return {
        "base": revision,
        "added_files": sorted(current_tests - previous_tests),
        "removed_files": sorted(previous_tests - current_tests),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """
    输出 JSON 清点及全量检查结果；提供基准时附加文件增删清单。

    :param argv: 命令行参数；None 时使用进程参数
    :return: 无违规时为 0，有违规时为 1
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="附加相对该提交的文件增删摘要；不作为例外基线")
    args = parser.parse_args(argv)
    report = inventory(PROJECT_ROOT)
    report["violations"] = layout_violations(PROJECT_ROOT, report)
    if args.base:
        report["changes"] = compare_base(PROJECT_ROOT, args.base)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["violations"]))


if __name__ == "__main__":
    raise SystemExit(main())
