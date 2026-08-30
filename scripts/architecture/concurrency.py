"""枚举宿主原生并发原语并维护稳定 owner 事实。"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/concurrency-baseline.json"
SCAN_ROOTS = (
    "app/adapters",
    "app/api",
    "app/agent",
    "app/application",
    "app/chain",
    "app/command.py",
    "app/db",
    "app/domain",
    "app/foundation",
    "app/main.py",
    "app/monitor",
    "app/modules",
    "app/runtime",
    "app/scheduler",
    "app/startup",
    "app/workflow",
)

_CALL_SUFFIXES = frozenset({
    "create_task", "ensure_future", "to_thread", "run_in_executor", "run_in_threadpool",
})
_CONSTRUCTOR_NAMES = frozenset({
    "Thread", "Timer", "ThreadPoolExecutor", "ProcessPoolExecutor", "Process",
})


@dataclass(frozen=True, slots=True)
class ConcurrencyFact:
    """描述一个原生并发调用及其静态 owner。"""

    path: str
    line: int
    target: str
    owner: str

    @property
    def key(self) -> str:
        """返回跨运行稳定的事实键。"""
        return f"{self.path}:{self.line}:{self.target}"

    def as_dict(self) -> dict[str, str]:
        """转换为基线文件中的稳定记录。"""
        return {"owner": self.owner, "target": self.target}


def _iter_python_files(root: Path, scan_roots: Iterable[str]) -> Iterable[Path]:
    """遍历宿主源码并排除插件副本、SDK 和兼容实现。"""
    for scan_root in scan_roots:
        path = root / scan_root
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("*.py"))
        else:
            candidates = []
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            if any(
                relative == excluded or relative.startswith(f"{excluded}/")
                for excluded in ("app/plugins", "app/sdk", "app/runtime/compat")
            ):
                continue
            yield candidate


def _target_name(node: ast.Call) -> str | None:
    """识别约定的原生并发 API，避免按普通方法名误报。"""
    if not isinstance(node.func, (ast.Name, ast.Attribute)):
        return None
    target = ast.unparse(node.func)
    terminal = target.rsplit(".", 1)[-1]
    if terminal in _CALL_SUFFIXES or terminal in _CONSTRUCTOR_NAMES:
        return terminal
    return None


class _ConcurrencyVisitor(ast.NodeVisitor):
    """收集函数、类和模块级并发调用的词法 owner。"""

    def __init__(self, relative_path: str) -> None:
        """初始化文件位置和 owner 栈。"""
        self._relative_path = relative_path
        self._owners: list[str] = []
        self.facts: list[ConcurrencyFact] = []

    @property
    def _owner(self) -> str:
        """返回最具体的可定位 owner。"""
        return ".".join(self._owners) if self._owners else self._relative_path

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """把类名纳入 owner 路径后继续扫描方法体。"""
        self._owners.append(node.name)
        self.generic_visit(node)
        self._owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """把同步函数纳入 owner 路径后扫描其嵌套并发。"""
        self._owners.append(node.name)
        self.generic_visit(node)
        self._owners.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """把异步函数纳入 owner 路径后扫描其嵌套并发。"""
        self._owners.append(node.name)
        self.generic_visit(node)
        self._owners.pop()

    def visit_Call(self, node: ast.Call) -> None:
        """记录已识别原语并继续访问参数中的嵌套调用。"""
        target = _target_name(node)
        if target:
            self.facts.append(
                ConcurrencyFact(
                    path=self._relative_path,
                    line=node.lineno,
                    target=target,
                    owner=self._owner,
                )
            )
        self.generic_visit(node)


def collect_concurrency(
    root: Path = PROJECT_ROOT,
    *,
    scan_roots: Iterable[str] = SCAN_ROOTS,
) -> dict[str, dict[str, str]]:
    """收集全部宿主原生并发调用及静态 owner。"""
    facts: dict[str, dict[str, str]] = {}
    for path in _iter_python_files(root, scan_roots):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        visitor = _ConcurrencyVisitor(relative)
        visitor.visit(tree)
        for fact in visitor.facts:
            facts[fact.key] = fact.as_dict()
    return dict(sorted(facts.items()))


def compare_concurrency(
    baseline: dict[str, dict[str, str]],
    current: dict[str, dict[str, str]],
) -> list[str]:
    """拒绝新增原语、owner 丢失或事实 owner 漂移。"""
    problems: list[str] = []
    for key, record in current.items():
        if not record.get("owner"):
            problems.append(f"并发事实缺少 owner：{key}")
            continue
        previous = baseline.get(key)
        if previous is None:
            problems.append(f"新增原生并发事实：{key}")
        elif previous.get("owner") != record.get("owner"):
            problems.append(
                f"并发 owner 漂移：{key}={record.get('owner')}（基线为 {previous.get('owner')}）"
            )
    return problems


def main() -> int:
    """执行原生并发事实 ratchet。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写入当前并发事实基线")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    current = collect_concurrency()
    if args.write:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {args.baseline.relative_to(PROJECT_ROOT)}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = compare_concurrency(baseline, current)
    if problems:
        print("\n".join(problems))
        return 1
    print("原生并发事实 ratchet 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
