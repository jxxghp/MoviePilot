"""枚举宿主原生并发原语并维护稳定 owner 事实。"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TypedDict, cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/concurrency-baseline.json"
SCAN_ROOTS = ("app",)
EXCLUDED_ROOTS = ("app/plugins", "app/sdk", "app/runtime/compat")

_RECORDED_CALLS = frozenset({
    "app.runtime.execution.run_in_threadpool",
    "app.runtime.execution.run_in_threadpool_to_completion",
    "asyncio.TaskGroup",
    "asyncio.create_task",
    "asyncio.ensure_future",
    "asyncio.to_thread",
    "concurrent.futures.ProcessPoolExecutor",
    "concurrent.futures.ThreadPoolExecutor",
    "multiprocessing.Process",
    "starlette.concurrency.run_in_threadpool",
    "threading.Thread",
    "threading.Timer",
})
_LOOP_FACTORIES = frozenset({
    "asyncio.get_event_loop",
    "asyncio.get_running_loop",
    "asyncio.new_event_loop",
})
_LOOP_TYPES = frozenset({"asyncio.AbstractEventLoop", "asyncio.BaseEventLoop"})
_EXECUTOR_TYPES = frozenset({
    "concurrent.futures.Executor",
    "concurrent.futures.ProcessPoolExecutor",
    "concurrent.futures.ThreadPoolExecutor",
})
_EVENT_LOOP_KIND = "asyncio.AbstractEventLoop"
_TASK_GROUP_KIND = "asyncio.TaskGroup"
_EXECUTOR_KIND = "concurrent.futures.Executor"
_RECEIVER_CALLS = {
    (_EVENT_LOOP_KIND, "create_task"): "asyncio.AbstractEventLoop.create_task",
    (_EVENT_LOOP_KIND, "run_in_executor"): "asyncio.AbstractEventLoop.run_in_executor",
    (_TASK_GROUP_KIND, "create_task"): "asyncio.TaskGroup.create_task",
    (_EXECUTOR_KIND, "map"): "concurrent.futures.Executor.map",
    (_EXECUTOR_KIND, "submit"): "concurrent.futures.Executor.submit",
}


class ConcurrencyRecord(TypedDict):
    """描述一种原生并发调用在文件内的 owner 计数。"""

    target: str
    owners: dict[str, int]


@dataclass(frozen=True, slots=True)
class ConcurrencyFact:
    """描述一个已确认来源的原生并发调用及其静态 owner。"""

    path: str
    target: str
    owner: str

    @property
    def key(self) -> str:
        """返回不依赖物理行号的事实键。"""
        return f"{self.path}:{self.target}"


def _iter_python_files(root: Path, scan_roots: Iterable[str]) -> Iterable[Path]:
    """遍历完整宿主源码，并只排除有独立治理契约的兼容表面。"""
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
                for excluded in EXCLUDED_ROOTS
            ):
                continue
            yield candidate


class _ConcurrencyVisitor(ast.NodeVisitor):
    """按 canonical import、局部来源和词法 owner 收集并发调用。"""

    def __init__(self, relative_path: str) -> None:
        """初始化文件位置、owner 栈和逐作用域名称绑定。"""
        self._relative_path = relative_path
        self._owners: list[str] = []
        self._class_owners: list[str] = []
        self._bindings: list[dict[str, str | None]] = [{}]
        self._attribute_bindings: dict[tuple[str, str], str] = {}
        self.facts: list[ConcurrencyFact] = []

    @property
    def _owner(self) -> str:
        """返回最具体的可定位 owner。"""
        return ".".join(self._owners) if self._owners else "<module>"

    @property
    def _class_owner(self) -> str:
        """返回当前类路径，用于区分不同类的实例属性。"""
        return ".".join(self._class_owners)

    def _bind_name(self, name: str, origin: str | None) -> None:
        """在当前作用域绑定名称；None 表示显式遮蔽外层导入。"""
        self._bindings[-1][name] = origin

    def _lookup_name(self, name: str) -> str | None:
        """按 Python 词法作用域顺序解析名称来源。"""
        for scope in reversed(self._bindings):
            if name in scope:
                return scope[name]
        return None

    def _attribute_key(self, node: ast.Attribute) -> tuple[str, str]:
        """为实例属性生成带类 owner 的稳定绑定键。"""
        return self._class_owner, ast.unparse(node)

    def _resolve_expression(self, node: ast.AST) -> str | None:
        """解析名称、属性或构造结果的 canonical 来源。"""
        if isinstance(node, ast.Name):
            return self._lookup_name(node.id)
        if isinstance(node, ast.Attribute):
            bound = self._attribute_bindings.get(self._attribute_key(node))
            if bound:
                return bound
            base = self._resolve_expression(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call):
            target = self._resolve_expression(node.func)
            if target in _LOOP_FACTORIES:
                return _EVENT_LOOP_KIND
            if target == _TASK_GROUP_KIND:
                return _TASK_GROUP_KIND
            if target in _EXECUTOR_TYPES:
                return _EXECUTOR_KIND
        return None

    def _resolve_call_target(self, node: ast.Call) -> str | None:
        """仅在调用来源可证明时返回受治理的 canonical target。"""
        direct = self._resolve_expression(node.func)
        if direct in _RECORDED_CALLS:
            return direct
        if isinstance(node.func, ast.Attribute):
            receiver = self._resolve_expression(node.func.value)
            return _RECEIVER_CALLS.get((receiver or "", node.func.attr))
        return None

    def _bind_target(self, target: ast.expr, origin: str | None) -> None:
        """把赋值目标关联到可证明来源，并显式遮蔽未知名称。"""
        if isinstance(target, ast.Name):
            self._bind_name(target.id, origin)
        elif isinstance(target, ast.Attribute):
            key = self._attribute_key(target)
            if origin:
                self._attribute_bindings[key] = origin
            else:
                self._attribute_bindings.pop(key, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._bind_target(item, None)

    def _bind_annotated_arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """根据参数注解标记事件循环、TaskGroup 和 Executor 来源。"""
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.annotation is None:
                continue
            origins = {
                origin
                for item in ast.walk(argument.annotation)
                if (origin := self._resolve_expression(item))
            }
            if origins & _LOOP_TYPES:
                self._bind_name(argument.arg, _EVENT_LOOP_KIND)
            elif _TASK_GROUP_KIND in origins:
                self._bind_name(argument.arg, _TASK_GROUP_KIND)
            elif origins & _EXECUTOR_TYPES:
                self._bind_name(argument.arg, _EXECUTOR_KIND)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """进入函数 owner 和局部作用域后扫描全部子节点。"""
        self._bind_name(node.name, None)
        self._owners.append(node.name)
        self._bindings.append({})
        self._bind_annotated_arguments(node)
        self.generic_visit(node)
        self._bindings.pop()
        self._owners.pop()

    def visit_Import(self, node: ast.Import) -> None:
        """登记模块导入及其别名。"""
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            origin = alias.name if alias.asname else bound_name
            self._bind_name(bound_name, origin)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """登记直接导入符号及其别名。"""
        if not node.module:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            self._bind_name(alias.asname or alias.name, f"{node.module}.{alias.name}")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """把类名纳入 owner 路径并隔离类体名称绑定。"""
        self._bind_name(node.name, None)
        self._owners.append(node.name)
        self._class_owners.append(node.name)
        self._bindings.append({})
        self.generic_visit(node)
        self._bindings.pop()
        self._class_owners.pop()
        self._owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """扫描同步函数及其嵌套 owner。"""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """扫描异步函数及其嵌套 owner。"""
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """记录赋值右侧调用并传播可证明的运行时来源。"""
        self.visit(node.value)
        origin = self._resolve_expression(node.value)
        for target in node.targets:
            self._bind_target(target, origin)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """处理带注解赋值并传播右侧来源。"""
        self.visit(node.annotation)
        if node.value:
            self.visit(node.value)
        self._bind_target(node.target, self._resolve_expression(node.value) if node.value else None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """处理海象表达式中的来源传播。"""
        self.visit(node.value)
        self._bind_target(node.target, self._resolve_expression(node.value))

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        """先解析上下文管理器，再让其绑定在语句体内可见。"""
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target(item.optional_vars, self._resolve_expression(item.context_expr))
        for statement in node.body:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        """扫描同步上下文管理器及其绑定。"""
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """扫描异步上下文管理器及其绑定。"""
        self._visit_with(node)

    def visit_Call(self, node: ast.Call) -> None:
        """记录来源明确的原语并继续访问参数中的嵌套调用。"""
        target = self._resolve_call_target(node)
        if target:
            self.facts.append(
                ConcurrencyFact(
                    path=self._relative_path,
                    target=target,
                    owner=self._owner,
                )
            )
        self.generic_visit(node)


def collect_concurrency(
    root: Path = PROJECT_ROOT,
    *,
    scan_roots: Iterable[str] = SCAN_ROOTS,
) -> dict[str, ConcurrencyRecord]:
    """按文件、canonical target 和 owner 计数收集原生并发事实。"""
    records: dict[str, ConcurrencyRecord] = {}
    for path in _iter_python_files(root, scan_roots):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        visitor = _ConcurrencyVisitor(relative)
        visitor.visit(tree)
        for fact in visitor.facts:
            record = records.setdefault(fact.key, {"target": fact.target, "owners": {}})
            record["owners"][fact.owner] = record["owners"].get(fact.owner, 0) + 1
    return dict(sorted(records.items()))


def compare_concurrency(
    baseline: dict[str, ConcurrencyRecord],
    current: dict[str, ConcurrencyRecord],
) -> list[str]:
    """拒绝新增/增长，并要求事实减少时同步下调低水位基线。"""
    problems: list[str] = []
    for key in sorted(baseline.keys() | current.keys()):
        previous = baseline.get(key)
        record = current.get(key)
        if previous is None and record is not None:
            problems.append(f"新增原生并发事实：{key} owners={record['owners']}")
            continue
        if record is None and previous is not None:
            problems.append(f"并发低水位已下降，请刷新基线：{key}")
            continue
        if previous is None or record is None:
            continue
        if record.get("target") != previous.get("target"):
            problems.append(f"并发 target 漂移：{key}")
        previous_owners = previous.get("owners", {})
        current_owners = record.get("owners", {})
        for owner in sorted(previous_owners.keys() | current_owners.keys()):
            old_count = previous_owners.get(owner, 0)
            new_count = current_owners.get(owner, 0)
            if not owner:
                problems.append(f"并发事实缺少 owner：{key}")
            elif new_count > old_count:
                problems.append(
                    f"并发调用新增或增长：{key} owner={owner} {new_count}>{old_count}"
                )
            elif new_count < old_count:
                problems.append(
                    f"并发低水位已下降，请刷新基线：{key} owner={owner} {new_count}<{old_count}"
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
    baseline = cast(
        dict[str, ConcurrencyRecord],
        json.loads(args.baseline.read_text(encoding="utf-8")),
    )
    problems = compare_concurrency(baseline, current)
    if problems:
        print("\n".join(problems))
        return 1
    print("原生并发事实 ratchet 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
