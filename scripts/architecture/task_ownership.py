"""检查宿主 TaskRegistry 调用是否声明稳定的任务 owner。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_METHODS = frozenset({"create", "create_sync", "register"})
TASK_MODULE = "app.runtime.tasks"
CONTEXT_MODULE = "app.api.context"
TASK_FACTORIES = frozenset(
    {
        "get_task_registry",
        "get_background_task_registry",
        "get_background_task_registry_compat",
        "resolve_background_task_registry",
    }
)
EXCLUDED_ROOTS = (
    "app/plugins",
    "app/runtime/compat",
    "app/sdk",
    "app/testing",
)


@dataclass(frozen=True, order=True, slots=True)
class TaskOwnerViolation:
    """描述一处缺少稳定字符串 owner 的 TaskRegistry 调用。"""

    path: str
    line: int
    method: str
    reason: str

    def render(self) -> str:
        """返回适合 CI 输出的稳定诊断文本。"""
        return f"{self.path}:{self.line}: TaskRegistry.{self.method} {self.reason}"


@dataclass(slots=True)
class _Scope:
    """保存当前词法作用域内可确认来源的任务登记器符号。"""

    task_classes: set[str] = field(default_factory=set)
    task_factories: set[str] = field(default_factory=set)
    module_aliases: dict[str, str] = field(default_factory=dict)
    registry_names: set[str] = field(default_factory=set)

    def child(self) -> _Scope:
        """复制父级可见绑定，供嵌套函数或类独立追踪局部赋值。"""
        return _Scope(
            task_classes=set(self.task_classes),
            task_factories=set(self.task_factories),
            module_aliases=dict(self.module_aliases),
            registry_names=set(self.registry_names),
        )


class _TaskOwnershipVisitor(ast.NodeVisitor):
    """仅跟踪可由 import、类型注解或工厂调用确认的 TaskRegistry。"""

    def __init__(self, relative_path: str) -> None:
        """初始化源码位置、词法作用域和违规记录。"""
        self._relative_path = relative_path
        self._scopes = [_Scope()]
        self.violations: list[TaskOwnerViolation] = []

    @property
    def _scope(self) -> _Scope:
        """返回当前词法作用域。"""
        return self._scopes[-1]

    def _visit_nested_scope(
        self,
        statements: list[ast.stmt],
        arguments: ast.arguments | None = None,
    ) -> None:
        """在继承可见符号的新作用域中访问函数、类或 lambda 主体。"""
        self._scopes.append(self._scope.child())
        try:
            if arguments is not None:
                self._bind_arguments(arguments)
            for statement in statements:
                self.visit(statement)
        finally:
            self._scopes.pop()

    def _bind_arguments(self, arguments: ast.arguments) -> None:
        """把明确标注为 TaskRegistry 的函数参数加入当前作用域。"""
        positional = (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        for argument in positional:
            self._bind_name(
                argument.arg,
                self._is_registry_annotation(argument.annotation),
            )
        if arguments.vararg:
            self._bind_name(
                arguments.vararg.arg,
                self._is_registry_annotation(arguments.vararg.annotation),
            )
        if arguments.kwarg:
            self._bind_name(
                arguments.kwarg.arg,
                self._is_registry_annotation(arguments.kwarg.annotation),
            )

    def _bind_name(self, name: str, is_registry: bool) -> None:
        """更新局部名称的 TaskRegistry 绑定，显式重赋值会清除旧绑定。"""
        if is_registry:
            self._scope.registry_names.add(name)
        else:
            self._scope.registry_names.discard(name)

    def _bind_target(self, target: ast.expr, is_registry: bool) -> None:
        """处理普通名称和解构赋值产生的局部绑定。"""
        if isinstance(target, ast.Name):
            self._bind_name(target.id, is_registry)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                self._bind_target(item, False)

    def _is_registry_annotation(self, annotation: ast.expr | None) -> bool:
        """识别 TaskRegistry、联合类型和 Annotated 中的明确类型来源。"""
        if annotation is None:
            return False
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return False
        for node in ast.walk(annotation):
            if isinstance(node, ast.Name) and node.id in self._scope.task_classes:
                return True
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and self._scope.module_aliases.get(node.value.id) == TASK_MODULE
                and node.attr == "TaskRegistry"
            ):
                return True
        return False

    def _is_registry_factory(self, expression: ast.expr) -> bool:
        """判断调用目标是否是明确导入的登记器构造器或解析工厂。"""
        if isinstance(expression, ast.Name):
            return expression.id in (
                self._scope.task_classes | self._scope.task_factories
            )
        if isinstance(expression, ast.Attribute) and isinstance(
            expression.value, ast.Name
        ):
            module = self._scope.module_aliases.get(expression.value.id)
            if module == TASK_MODULE:
                return expression.attr in {"TaskRegistry", "get_task_registry"}
            if module == CONTEXT_MODULE:
                return expression.attr in TASK_FACTORIES
        return False

    def _is_registry_expression(self, expression: ast.expr | None) -> bool:
        """判断表达式是否确定返回或引用 TaskRegistry。"""
        if isinstance(expression, ast.Name):
            return expression.id in self._scope.registry_names
        if isinstance(expression, ast.Call):
            return self._is_registry_factory(expression.func)
        if isinstance(expression, ast.IfExp):
            return self._is_registry_expression(
                expression.body
            ) and self._is_registry_expression(expression.orelse)
        return False

    def _check_owner(self, node: ast.Call, method: str) -> None:
        """要求 owner 以显式、非空字符串字面量传入。"""
        owner = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "owner"),
            None,
        )
        if owner is None:
            reason = "缺少显式 owner"
        elif not (
            isinstance(owner, ast.Constant)
            and isinstance(owner.value, str)
            and owner.value.strip()
        ):
            reason = "的 owner 必须是非空字符串字面量"
        else:
            return
        self.violations.append(
            TaskOwnerViolation(
                path=self._relative_path,
                line=node.lineno,
                method=method,
                reason=reason,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        """记录 TaskRegistry 与 API context 模块别名。"""
        for alias in node.names:
            if alias.name not in {TASK_MODULE, CONTEXT_MODULE}:
                continue
            local_name = alias.asname or alias.name.split(".")[0]
            self._scope.module_aliases[local_name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """记录明确导入的 TaskRegistry 类和登记器工厂别名。"""
        if node.module == TASK_MODULE:
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name == "TaskRegistry":
                    self._scope.task_classes.add(local_name)
                elif alias.name == "get_task_registry":
                    self._scope.task_factories.add(local_name)
        elif node.module == CONTEXT_MODULE:
            for alias in node.names:
                if alias.name in TASK_FACTORIES:
                    self._scope.task_factories.add(alias.asname or alias.name)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        """在隔离的函数作用域中追踪参数和局部登记器。"""
        for expression in (*node.decorator_list, *node.args.defaults):
            self.visit(expression)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        self._visit_nested_scope(node.body, node.args)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """分析同步函数作用域。"""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """按与同步函数相同的规则追踪异步函数作用域。"""
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """隔离类体局部名称，同时保留模块导入的符号来源。"""
        for expression in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(expression)
        self._visit_nested_scope(node.body)

    def visit_Assign(self, node: ast.Assign) -> None:
        """传播由登记器构造或解析工厂建立的简单赋值。"""
        self.visit(node.value)
        is_registry = self._is_registry_expression(node.value)
        for target in node.targets:
            self._bind_target(target, is_registry)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """优先使用明确 TaskRegistry 注解，并兼容带初始值的赋值。"""
        if node.value is not None:
            self.visit(node.value)
        self._bind_target(
            node.target,
            self._is_registry_annotation(node.annotation)
            or self._is_registry_expression(node.value),
        )

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """传播海象表达式建立的登记器局部绑定。"""
        self.visit(node.value)
        self._bind_target(node.target, self._is_registry_expression(node.value))

    def visit_Call(self, node: ast.Call) -> None:
        """只校验接收者已被证明为 TaskRegistry 的目标方法调用。"""
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in TASK_METHODS
            and self._is_registry_expression(node.func.value)
        ):
            self._check_owner(node, node.func.attr)
        self.generic_visit(node)


def _is_excluded(relative_path: str) -> bool:
    """排除插件、SDK、兼容层与扫描器实现自身等非宿主调用面。"""
    if relative_path == "app/runtime/tasks.py":
        return True
    return any(
        relative_path == root or relative_path.startswith(f"{root}/")
        for root in EXCLUDED_ROOTS
    )


def collect_task_owner_violations(
    root: Path = PROJECT_ROOT,
) -> list[TaskOwnerViolation]:
    """扫描 canonical 宿主源码并返回缺少稳定 owner 的调用。"""
    violations: list[TaskOwnerViolation] = []
    for path in sorted((root / "app").rglob("*.py")):
        relative_path = path.relative_to(root).as_posix()
        if _is_excluded(relative_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        visitor = _TaskOwnershipVisitor(relative_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return sorted(violations)


def main() -> int:
    """执行 TaskRegistry owner 零债务门禁。"""
    violations = collect_task_owner_violations()
    if violations:
        print("\n".join(violation.render() for violation in violations))
        return 1
    print("TaskRegistry owner 门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
