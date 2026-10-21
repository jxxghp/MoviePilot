"""
锁定宿主的精确类型兼容例外，禁止新增忽略、类型桩和检查范围退化。
"""

from __future__ import annotations

import argparse
import ast
import configparser
import copy
import hashlib
import io
import json
import os
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = PROJECT_ROOT / "tests/fixtures/architecture/mypy-policy.json"
_IGNORE = re.compile(r"#\s*type:\s*ignore(?:\[([^\]]+)\])?")
_SKIP_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache",
})


def configuration(root: Path) -> dict[str, dict[str, str]]:
    """
    读取完整 mypy 配置，新增排除项和局部宽松选项必须经过显式复核。
    """
    parser = configparser.ConfigParser(interpolation=None)
    with (root / "mypy.ini").open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return {section: dict(sorted(parser.items(section))) for section in sorted(parser.sections())}


def _statements(tree: ast.AST, owner: str = "<module>") -> list[tuple[ast.stmt, str]]:
    """
    给每条语句标记所属类和方法，移动忽略到其他方法不能复用原额度。
    """
    result = []
    if isinstance(tree, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = tree.name if owner == "<module>" else f"{owner}.{tree.name}"
    if isinstance(tree, ast.stmt):
        result.append((tree, owner))
    for child in ast.iter_child_nodes(tree):
        result.extend(_statements(child, owner))
    return result


def _statement_at(statements: list[tuple[ast.stmt, str]], line: int) -> tuple[ast.stmt, str]:
    """
    选择忽略所在的最小语句；定义行只锁定完整签名，不把整个方法当作豁免。
    """
    candidates = []
    for node, owner in statements:
        start = node.lineno
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            start = min(start, *(item.lineno for item in node.decorator_list))
        if start <= line <= (node.end_lineno or start):
            candidates.append((node, owner, start))
    if not candidates:
        raise ValueError(f"第 {line} 行的类型忽略没有对应语句，禁止文件级忽略")
    node, owner, _ = min(candidates, key=lambda item: (item[0].end_lineno or item[2]) - item[2])
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.body and line >= node.body[0].lineno:
            raise ValueError(f"第 {line} 行禁止使用独立的类型忽略")
        node = copy.copy(node)
        node.body = []
    return node, owner


def _reject_unchecked_decorators(tree: ast.Module, path: str) -> None:
    """
    拒绝直接、别名或模块限定的 no_type_check 装饰器，不能跳过整个方法。
    """
    names = {"no_type_check", "no_type_check_decorator"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
            names.update(alias.asname or alias.name for alias in node.names if alias.name in names)
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in names:
                    raise ValueError(f"{path}:{decorator.lineno}: 禁止 no_type_check 装饰器")


def source_ignores(path: Path, root: Path) -> list[dict[str, Any]]:
    """
    用语法结构与错误码生成忽略身份，普通换行不会改变身份，语义变化必须复核。
    """
    source = path.read_text(encoding="utf-8")
    relative = path.relative_to(root).as_posix()
    tree = ast.parse(source, filename=relative)
    _reject_unchecked_decorators(tree, relative)
    statements = _statements(tree)
    result = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        if re.match(r"#\s*mypy\s*:", token.string):
            raise ValueError(f"{relative}:{token.start[0]}: 禁止文件内 mypy 配置")
        match = _IGNORE.match(token.string)
        if match is None:
            continue
        if not match.group(1):
            raise ValueError(f"{relative}:{token.start[0]}: 类型忽略必须指定错误码")
        codes = sorted(code.strip() for code in match.group(1).split(","))
        if not all(re.fullmatch(r"[a-z][a-z0-9-]*", code) for code in codes):
            raise ValueError(f"{relative}:{token.start[0]}: 无效的类型错误码")
        node, owner = _statement_at(statements, token.start[0])
        signature = ast.unparse(node)
        digest = hashlib.sha256((ast.dump(node) + json.dumps(codes)).encode()).hexdigest()
        result.append({"path": relative, "owner": owner, "codes": codes,
                       "fingerprint": digest, "statement": signature})
    return result


def collect(root: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    扫描全部宿主文件和本地类型桩，新文件自动纳入；运行时插件副本单独治理。
    """
    ignores = []
    stubs = {}
    for directory, children, filenames in os.walk(root):
        children[:] = sorted(name for name in children if name not in _SKIP_DIRECTORIES)
        parent = Path(directory)
        relative = parent.relative_to(root)
        if relative.parts[:2] == ("app", "plugins"):
            children[:] = []
            continue
        for name in sorted(filenames):
            path = parent / name
            if path.suffix == ".pyi":
                stubs[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            if relative.parts[:1] == ("app",) and path.suffix in {".py", ".pyi"}:
                ignores.extend(source_ignores(path, root))
    return ignores, dict(sorted(stubs.items()))


def _identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    """
    按文件、所属方法和完整语句指纹比较，不提供可在方法间转移的数量预算。
    """
    return entry["path"], entry["owner"], entry["fingerprint"]


def compare(
    root: Path, policy: dict[str, Any], ignores: list[dict[str, Any]], stubs: dict[str, str],
) -> tuple[list[str], list[str]]:
    """
    区分新增退化与已删除例外，只有后者允许用 --write 固化。
    """
    regressions = []
    stale = []
    if configuration(root) != policy["configuration"]:
        regressions.append("mypy 配置发生变化：检查范围和依赖类型边界需要复核")
    reasons = policy["reasons"]
    registered = policy["ignores"]
    for entry in [*registered, *policy["stubs"]]:
        if not reasons.get(entry.get("reason")):
            regressions.append(f"{entry['path']}: 类型例外缺少登记原因")
    old = Counter(_identity(entry) for entry in registered)
    new = Counter(_identity(entry) for entry in ignores)
    for (path, owner, _), count in sorted((new - old).items()):
        regressions.append(f"{path}:{owner}: 新增或变化的类型忽略 x{count}")
    for (path, owner, _), count in sorted((old - new).items()):
        stale.append(f"{path}:{owner}: 已移除类型忽略 x{count}，需要固化低水位")
    old_stubs = {entry["path"]: entry["sha256"] for entry in policy["stubs"]}
    for path, digest in stubs.items():
        if old_stubs.get(path) != digest:
            regressions.append(f"{path}: 新增或变化的本地类型桩，必须复核真实接口")
    for path in old_stubs.keys() - stubs.keys():
        stale.append(f"{path}: 已移除类型桩，需要固化低水位")
    return regressions, stale


def reduce_policy(policy: dict[str, Any], ignores: list[dict[str, Any]], stubs: dict[str, str]) -> dict[str, Any]:
    """
    只删除已消除的登记项，不接受新增忽略或新类型桩。
    """
    result = copy.deepcopy(policy)
    remaining = Counter(_identity(entry) for entry in ignores)
    kept = []
    for entry in policy["ignores"]:
        identity = _identity(entry)
        if remaining[identity]:
            kept.append(entry)
            remaining[identity] -= 1
    result["ignores"] = kept
    result["stubs"] = [entry for entry in policy["stubs"] if entry["path"] in stubs]
    used_reasons = {entry["reason"] for entry in [*result["ignores"], *result["stubs"]]}
    result["reasons"] = {name: value for name, value in policy["reasons"].items() if name in used_reasons}
    return result


def main() -> int:
    """
    检查精确例外，或在没有退化时删除失效登记；不提供自动接收例外的写入入口。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="仅固化已消除的类型例外")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        ignores, stubs = collect(PROJECT_ROOT)
        regressions, stale = compare(PROJECT_ROOT, policy, ignores, stubs)
    except (OSError, ValueError, KeyError) as error:
        print(f"类型例外门禁无法完成检查：{error}")
        return 1
    if regressions:
        print("\n".join(regressions))
        print("禁止扩大类型豁免；--write 只能删除已经消除的例外。")
        return 1
    if args.write:
        args.policy.write_text(json.dumps(reduce_policy(policy, ignores, stubs), ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    elif stale:
        print("\n".join(stale))
        return 1
    print(f"类型例外门禁通过（{len(ignores)} 处精确忽略，{len(stubs)} 份已核对类型桩）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
