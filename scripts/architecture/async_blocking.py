"""检测关键 async 路径中新引入的直接阻塞调用。"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/async-blocking-baseline.json"
SCAN_ROOTS = (
    "app/adapters",
    "app/api",
    "app/agent",
    "app/application",
    "app/chain",
    "app/db",
    "app/doctor",
    "app/domain",
    "app/foundation",
    "app/monitor",
    "app/modules",
    "app/runtime",
    "app/schemas",
    "app/startup",
    "app/workflow",
    "app/cli.py",
    "app/command.py",
    "app/factory.py",
    "app/main.py",
    "app/scheduler.py",
)

_SYNC_HTTP_METHODS = {
    "delete_res",
    "get",
    "get_json",
    "get_res",
    "get_stream",
    "post",
    "post_json",
    "post_res",
    "put",
    "put_res",
    "request",
    "response_manager",
}
_REQUESTS_METHODS = {
    "delete",
    "get",
    "head",
    "patch",
    "post",
    "put",
    "request",
}
_PATH_IO_METHODS = {
    "exists",
    "glob",
    "is_dir",
    "is_file",
    "iterdir",
    "mkdir",
    "open",
    "read_bytes",
    "read_text",
    "rename",
    "rglob",
    "stat",
    "unlink",
    "write_bytes",
    "write_text",
}
_SHUTIL_METHODS = {
    "copy",
    "copyfile",
    "copytree",
    "make_archive",
    "move",
    "rmtree",
    "unpack_archive",
    "which",
}
_SUBPROCESS_METHODS = {
    "Popen",
    "call",
    "check_call",
    "check_output",
    "run",
}
_OS_IO_METHODS = {"listdir", "scandir", "walk"}
_SYSTEM_CONFIG_MEMORY_READS = {
    "app.db.oper.SystemConfigOper.all",
    "app.db.oper.SystemConfigOper.get",
    "app.db.oper.systemconfig.SystemConfigOper.all",
    "app.db.oper.systemconfig.SystemConfigOper.get",
}


@dataclass(frozen=True)
class _Binding:
    """描述由明确 import 或局部赋值建立的符号来源。"""

    family: str
    qualified_name: str
    kind: str = "module"


_UNKNOWN_BINDING = _Binding("unknown", "", "unknown")
_BLOCKING_FAMILIES = {
    "os",
    "requests",
    "shutil",
    "subprocess",
    "sync_http",
    "sync_oper",
    "sync_path",
    "time",
}


def _binding_for_qualified(qualified_name: str) -> _Binding:
    """按稳定模块路径识别门禁关心的符号族。"""
    if qualified_name == "app.adapters.network.http.RequestUtils":
        return _Binding("sync_http", qualified_name, "class")
    if qualified_name == "app.adapters.network.http.AsyncRequestUtils":
        return _Binding("async_http", qualified_name, "class")
    if qualified_name in {"requests.Session", "requests.sessions.Session"}:
        return _Binding("sync_http", qualified_name, "class")
    if qualified_name == "pathlib.Path":
        return _Binding("sync_path", qualified_name, "class")
    if qualified_name == "anyio.Path":
        return _Binding("async_path", qualified_name, "class")
    if qualified_name.startswith("app.db.oper.") and qualified_name.endswith("Oper"):
        return _Binding("sync_oper", qualified_name, "class")
    if qualified_name.startswith("requests."):
        return _Binding("requests", qualified_name, "callable")
    if qualified_name.startswith("shutil."):
        return _Binding("shutil", qualified_name, "callable")
    if qualified_name.startswith("subprocess."):
        return _Binding("subprocess", qualified_name, "callable")
    if qualified_name.startswith("os."):
        return _Binding("os", qualified_name, "callable")
    if qualified_name.startswith("time."):
        return _Binding("time", qualified_name, "callable")
    return _Binding("module", qualified_name)


def _resolve_binding(
    expression: ast.expr,
    bindings: dict[str, _Binding],
) -> _Binding | None:
    """解析明确 import、构造、属性访问和简单路径派生。"""
    if isinstance(expression, ast.Name):
        return bindings.get(expression.id)
    if isinstance(expression, ast.Attribute):
        base = _resolve_binding(expression.value, bindings)
        if not base or base.kind in {"collection", "unknown"}:
            return None
        qualified_name = f"{base.qualified_name}.{expression.attr}"
        if base.kind == "module":
            return _binding_for_qualified(qualified_name)
        return _Binding(base.family, qualified_name, "callable")
    if isinstance(expression, ast.Call):
        target = _resolve_binding(expression.func, bindings)
        if target and target.kind == "class":
            return _Binding(target.family, target.qualified_name, "instance")
        return None
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Div):
        left = _resolve_binding(expression.left, bindings)
        if left and left.family in {"async_path", "sync_path"}:
            return left
    if isinstance(expression, ast.IfExp):
        return _merge_binding_options(
            "",
            (
                _resolve_binding(expression.body, bindings),
                _resolve_binding(expression.orelse, bindings),
            ),
        )
    return None


def _merge_binding_options(
    name: str,
    options: Sequence[_Binding | None],
) -> _Binding | None:
    """控制流合流时保留任一分支可能进入的同步阻塞类型。"""
    if options and all(option == options[0] for option in options):
        return options[0]
    blocking = sorted(
        (
            option
            for option in options
            if option and option.family in _BLOCKING_FAMILIES
        ),
        key=lambda item: (item.family, item.qualified_name, item.kind),
    )
    if blocking:
        return blocking[0]
    if name == "open" and any(option is None for option in options):
        return None
    return _UNKNOWN_BINDING


def _merge_binding_states(
    states: Sequence[dict[str, _Binding]],
) -> dict[str, _Binding]:
    """合并互斥控制流的局部符号表。"""
    names = set().union(*(state.keys() for state in states))
    merged: dict[str, _Binding] = {}
    for name in names:
        binding = _merge_binding_options(name, tuple(state.get(name) for state in states))
        if binding:
            merged[name] = binding
    return merged


def _load_oper_methods(root: Path) -> dict[str, set[str]]:
    """从 Oper 源码建立同步方法索引，避免按方法名猜测数据库调用。"""
    methods: dict[str, set[str]] = {}
    oper_root = root / "app/db/oper"
    if not oper_root.is_dir():
        return methods
    for path in sorted(oper_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Oper"):
                continue
            qualified_name = f"{module}.{node.name}"
            sync_methods = {
                child.name
                for child in node.body
                if isinstance(child, ast.FunctionDef)
                and not child.name.startswith("__")
            }
            methods[qualified_name] = sync_methods
            methods[f"app.db.oper.{node.name}"] = sync_methods
    return methods


class _ImportCollector(ast.NodeVisitor):
    """收集模块级 import，作为每个 async 函数的初始符号表。"""

    def __init__(self) -> None:
        self.bindings: dict[str, _Binding] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            qualified_name = alias.name if alias.asname else local_name
            self.bindings[local_name] = _binding_for_qualified(qualified_name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            qualified_name = f"{node.module}.{alias.name}"
            self.bindings[local_name] = _binding_for_qualified(qualified_name)

    def _bind_target(self, target: ast.expr, binding: _Binding | None) -> None:
        if isinstance(target, ast.Name):
            self.bindings[target.id] = binding or _UNKNOWN_BINDING
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        binding = _resolve_binding(node.value, self.bindings)
        for target in node.targets:
            self._bind_target(target, binding)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        binding = _resolve_binding(node.value, self.bindings) if node.value else None
        self._bind_target(node.target, binding)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


class _AsyncCallVisitor(ast.NodeVisitor):
    """用局部符号传播识别一个 async 函数直接执行的阻塞调用。"""

    def __init__(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        module_bindings: dict[str, _Binding],
        oper_methods: dict[str, set[str]],
        *,
        record_calls: bool,
    ) -> None:
        self.calls: Counter[str] = Counter()
        self.nested_functions: list[
            tuple[ast.FunctionDef | ast.AsyncFunctionDef, dict[str, _Binding]]
        ] = []
        self._bindings = dict(module_bindings)
        self._oper_methods = oper_methods
        self._record_calls = record_calls
        self._bind_arguments(function)

    def _bind_arguments(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        arguments = (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
        for argument in arguments:
            binding = self._annotation_binding(argument.annotation)
            if binding:
                self._bindings[argument.arg] = binding
            else:
                self._bindings[argument.arg] = _UNKNOWN_BINDING

    def _annotation_binding(self, annotation: ast.expr | None) -> _Binding | None:
        if annotation is None:
            return None
        if isinstance(annotation, ast.Subscript):
            container = ast.unparse(annotation.value).rsplit(".", 1)[-1]
            elements = (
                annotation.slice.elts
                if isinstance(annotation.slice, ast.Tuple)
                else (annotation.slice,)
            )
            for element in elements:
                binding = self._annotation_binding(element)
                if not binding:
                    continue
                if container in {"list", "List", "Sequence", "set", "tuple"}:
                    return _Binding(
                        binding.family,
                        binding.qualified_name,
                        "collection",
                    )
                if container in {"Annotated", "Optional", "Union"}:
                    return binding
            return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_binding(annotation.left) or self._annotation_binding(
                annotation.right
            )
        binding = _resolve_binding(annotation, self._bindings)
        if binding and binding.kind == "class":
            return _Binding(binding.family, binding.qualified_name, "instance")
        return None

    def _resolve(self, expression: ast.expr) -> _Binding | None:
        return _resolve_binding(expression, self._bindings)

    @staticmethod
    def _call_label(binding: _Binding) -> str:
        parts = binding.qualified_name.split(".")
        if binding.family == "sync_oper" and len(parts) >= 2:
            return ".".join(parts[-2:])
        if binding.family in {"async_path", "sync_path"} and parts:
            return f"Path.{parts[-1]}"
        if binding.family == "sync_http" and len(parts) >= 2:
            return ".".join(parts[-2:])
        if binding.family in {"os", "requests", "shutil", "subprocess", "time"}:
            return ".".join(parts[-2:])
        return binding.qualified_name

    def _record_call(self, node: ast.Call, binding: _Binding | None) -> None:
        if not self._record_calls:
            return
        if not binding:
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                self.calls["open"] += 1
            return
        method = binding.qualified_name.rsplit(".", 1)[-1]
        blocked = False
        if binding.family == "sync_http":
            blocked = binding.kind == "callable" and method in _SYNC_HTTP_METHODS
        elif binding.family == "requests":
            blocked = method in _REQUESTS_METHODS
        elif binding.family == "sync_path":
            blocked = binding.kind == "callable" and method in _PATH_IO_METHODS
        elif binding.family == "shutil":
            blocked = method in _SHUTIL_METHODS
        elif binding.family == "subprocess":
            blocked = method in _SUBPROCESS_METHODS
        elif binding.family == "os":
            blocked = method in _OS_IO_METHODS
        elif binding.family == "time":
            blocked = method == "sleep"
        elif binding.family == "sync_oper" and binding.kind == "callable":
            class_name, method_name = binding.qualified_name.rsplit(".", 1)
            blocked = (
                method_name in self._oper_methods.get(class_name, set())
                and binding.qualified_name not in _SYSTEM_CONFIG_MEMORY_READS
            )
        if blocked:
            self.calls[self._call_label(binding)] += 1

    def _bind_target(self, target: ast.expr, binding: _Binding | None) -> None:
        if isinstance(target, ast.Name):
            self._bindings[target.id] = binding or _UNKNOWN_BINDING
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element, None)

    def visit_Import(self, node: ast.Import) -> None:
        collector = _ImportCollector()
        collector.visit_Import(node)
        self._bindings.update(collector.bindings)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        collector = _ImportCollector()
        collector.visit_ImportFrom(node)
        self._bindings.update(collector.bindings)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        binding = self._resolve(node.value)
        for target in node.targets:
            self._bind_target(target, binding)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        binding = self._resolve(node.value) if node.value else None
        binding = binding or self._annotation_binding(node.annotation)
        self._bind_target(node.target, binding)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind_target(node.target, None)

    def _visit_branch(
        self,
        statements: Sequence[ast.stmt],
        initial: dict[str, _Binding],
    ) -> dict[str, _Binding]:
        saved = self._bindings
        self._bindings = dict(initial)
        for statement in statements:
            self.visit(statement)
        result = self._bindings
        self._bindings = saved
        return result

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        initial = dict(self._bindings)
        body = self._visit_branch(node.body, initial)
        alternate = self._visit_branch(node.orelse, initial) if node.orelse else initial
        self._bindings = _merge_binding_states((body, alternate))

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        initial = dict(self._bindings)
        collection = self._resolve(node.iter)
        element = (
            _Binding(collection.family, collection.qualified_name, "instance")
            if collection and collection.kind == "collection"
            else None
        )
        self._bind_target(node.target, element)
        for statement in node.body:
            self.visit(statement)
        iterated = dict(self._bindings)
        completed = self._visit_branch(node.orelse, iterated)
        self._bindings = _merge_binding_states((initial, completed))

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def _visit_comprehension(
        self,
        generators: Sequence[ast.comprehension],
        outputs: Sequence[ast.expr],
    ) -> None:
        saved = self._bindings
        self._bindings = dict(saved)
        for generator in generators:
            self.visit(generator.iter)
            collection = self._resolve(generator.iter)
            element = (
                _Binding(collection.family, collection.qualified_name, "instance")
                if collection and collection.kind == "collection"
                else None
            )
            self._bind_target(generator.target, element)
            for condition in generator.ifs:
                self.visit(condition)
        for output in outputs:
            self.visit(output)
        self._bindings = saved

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target(node.target, self._resolve(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        binding = self._resolve(node.func)
        self._record_call(node, binding)
        if isinstance(node.func, ast.Lambda):
            self._visit_lambda_defaults(node.func)
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            self.visit(node.func.body)
            return
        self.generic_visit(node)

    def _visit_definition_expressions(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for keyword_default in node.args.kw_defaults:
            if keyword_default:
                self.visit(keyword_default)

    def _visit_nested_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._visit_definition_expressions(node)
        self.nested_functions.append((node, dict(self._bindings)))
        self._bindings[node.name] = _UNKNOWN_BINDING

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_nested_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_nested_function(node)

    def _visit_lambda_defaults(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for keyword_default in node.args.kw_defaults:
            if keyword_default:
                self.visit(keyword_default)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_lambda_defaults(node)


def _root_functions(
    tree: ast.Module,
) -> Iterator[tuple[str, ast.AsyncFunctionDef]]:
    """产出模块顶层和类直接拥有的 async 入口。"""
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.AsyncFunctionDef):
                    yield f"{node.name}.{method.name}", method


def _scan_paths(root: Path, scan_roots: Sequence[str | Path]) -> Iterator[Path]:
    """按稳定顺序产出存在的扫描目标。"""
    for scan_root in scan_roots:
        target = root / scan_root
        if target.is_file():
            yield target
        elif target.is_dir():
            yield from sorted(target.rglob("*.py"))


def collect_async_blocking(
    root: Path = PROJECT_ROOT,
    scan_roots: Sequence[str | Path] = SCAN_ROOTS,
) -> dict[str, int]:
    """扫描关键目录并以文件、函数、调用名聚合存量次数。"""
    debt: Counter[str] = Counter()
    oper_methods = _load_oper_methods(root)
    for path in _scan_paths(root, scan_roots):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _ImportCollector()
        for statement in tree.body:
            imports.visit(statement)
        relative = path.relative_to(root).as_posix()
        pending: list[
            tuple[
                str,
                ast.FunctionDef | ast.AsyncFunctionDef,
                dict[str, _Binding],
            ]
        ] = [
            (qualname, function, imports.bindings)
            for qualname, function in _root_functions(tree)
        ]
        pending_index = 0
        while pending_index < len(pending):
            qualname, function, lexical_bindings = pending[pending_index]
            pending_index += 1
            is_async = isinstance(function, ast.AsyncFunctionDef)
            visitor = _AsyncCallVisitor(
                function,
                lexical_bindings,
                oper_methods,
                record_calls=is_async,
            )
            for statement in function.body:
                visitor.visit(statement)
            if is_async:
                for call_name, count in visitor.calls.items():
                    debt[f"{relative}:{qualname}:{call_name}"] += count
            pending.extend(
                (
                    f"{qualname}.{nested.name}",
                    nested,
                    bindings,
                )
                for nested, bindings in visitor.nested_functions
            )
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
