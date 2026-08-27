"""静态收集可证明的宿主 EventManager consumer。"""

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias

_DEFAULT_IDENTITY = "<default>"
_DYNAMIC_IDENTITY = "<dynamic>"
_EVENT_MANAGER_METHODS = {"add_event_listener", "register"}
_COMPREHENSION_SCOPES = (
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_FUNCTION_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    *_COMPREHENSION_SCOPES,
)


@dataclass(frozen=True, slots=True)
class _Symbol:
    """记录 canonical module、EventManager 类或实例的静态来源。"""

    kind: Literal[
        "module",
        "manager_class",
        "manager_factory",
        "manager_instance",
        "type_checking",
    ]
    value: str = ""


@dataclass(frozen=True, slots=True)
class _EventSelection:
    """记录一次注册可静态确定的事件集合及未知余项。"""

    events: tuple[str, ...]
    kind: Literal["member", "enum", "list"]
    dynamic: bool = False


@dataclass(frozen=True, slots=True)
class _DecoratorFactory:
    """记录尚未应用到 handler 的 EventManager.register 返回值。"""

    selection: _EventSelection
    priority: str


@dataclass(frozen=True, slots=True)
class _Registration:
    """记录已证明 receiver 后解析出的注册调用。"""

    method: Literal["add_event_listener", "register"]
    selection: _EventSelection
    handler: str
    priority: str


_ScopeValue: TypeAlias = _Symbol | _EventSelection | _DecoratorFactory | None


def _expression_name(node: ast.AST | None) -> str:
    """返回 Name/Attribute 表达式的稳定点分名称。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return ".".join(part for part in (prefix, node.attr) if part)
    return ""


def _handler_identity(node: ast.AST | None) -> str:
    """返回不含源码位置的 handler identity。"""
    if name := _expression_name(node):
        return name
    if isinstance(node, ast.Lambda):
        return "<lambda>"
    return _DYNAMIC_IDENTITY


def _priority_identity(node: ast.AST | None) -> str:
    """返回不含源码位置的 priority identity。"""
    if node is None:
        return _DEFAULT_IDENTITY
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if name := _expression_name(node):
        return name
    return _DYNAMIC_IDENTITY


def _location_sort_key(item: dict[str, Any]) -> tuple[str, int, str]:
    """返回 consumer location 的稳定排序键。"""
    return (
        str(item["caller"]),
        int(item["line"]),
        str(item["identity"]),
    )


def _bound_names(target: ast.AST) -> set[str]:
    """返回赋值目标在当前 lexical scope 绑定的名称。"""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {
            name
            for element in target.elts
            for name in _bound_names(element)
        }
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return set()


def _pattern_bound_names(pattern: ast.pattern) -> set[str]:
    """返回结构化匹配 pattern 捕获到当前 scope 的名称。"""
    if isinstance(pattern, ast.MatchAs):
        names = _pattern_bound_names(pattern.pattern) if pattern.pattern else set()
        if pattern.name:
            names.add(pattern.name)
        return names
    if isinstance(pattern, ast.MatchStar):
        return {pattern.name} if pattern.name else set()
    if isinstance(pattern, ast.MatchSequence):
        return {
            name
            for child in pattern.patterns
            for name in _pattern_bound_names(child)
        }
    if isinstance(pattern, ast.MatchMapping):
        names = {
            name
            for child in pattern.patterns
            for name in _pattern_bound_names(child)
        }
        if pattern.rest:
            names.add(pattern.rest)
        return names
    if isinstance(pattern, ast.MatchClass):
        return {
            name
            for child in (*pattern.patterns, *pattern.kwd_patterns)
            for name in _pattern_bound_names(child)
        }
    if isinstance(pattern, ast.MatchOr):
        return {
            name
            for child in pattern.patterns
            for name in _pattern_bound_names(child)
        }
    return set()


def _function_local_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """按 Python lexical scope 预收集函数局部绑定，避免回退到同名全局。"""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def belongs_to_function(
        candidate: ast.AST,
        *,
        cross_comprehensions: bool = False,
    ) -> bool:
        """判断节点是否属于当前函数而非嵌套 scope。"""
        parent = parents.get(candidate)
        while parent is not None and parent is not node:
            if isinstance(parent, _FUNCTION_SCOPES):
                if not (
                    cross_comprehensions
                    and isinstance(parent, _COMPREHENSION_SCOPES)
                ):
                    return False
            parent = parents.get(parent)
        return parent is node

    local_names = {
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    }
    if node.args.vararg:
        local_names.add(node.args.vararg.arg)
    if node.args.kwarg:
        local_names.add(node.args.kwarg.arg)
    global_names: set[str] = set()
    nonlocal_names: set[str] = set()
    for candidate in ast.walk(node):
        if candidate is node or not belongs_to_function(candidate):
            continue
        if isinstance(candidate, ast.Name) and isinstance(
            candidate.ctx,
            (ast.Store, ast.Del),
        ):
            local_names.add(candidate.id)
        elif isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_names.add(candidate.name)
        elif isinstance(candidate, (ast.Import, ast.ImportFrom)):
            for alias in candidate.names:
                if alias.name != "*":
                    local_names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(candidate, ast.ExceptHandler) and candidate.name:
            local_names.add(candidate.name)
        elif isinstance(candidate, ast.Match):
            local_names.update(
                name
                for case in candidate.cases
                for name in _pattern_bound_names(case.pattern)
            )
        elif isinstance(candidate, ast.Global):
            global_names.update(candidate.names)
        elif isinstance(candidate, ast.Nonlocal):
            nonlocal_names.update(candidate.names)
    for candidate in ast.walk(node):
        if isinstance(candidate, ast.NamedExpr) and belongs_to_function(
            candidate,
            cross_comprehensions=True,
        ):
            local_names.update(_bound_names(candidate.target))
    return local_names - global_names - nonlocal_names


class _EventConsumerCollector(ast.NodeVisitor):
    """以有限 lexical provenance 收集单个宿主模块的事件消费者。"""

    def __init__(
        self,
        module_name: str,
        event_members: dict[str, tuple[str, ...]],
        *,
        collect_facts: bool,
        module_final_scope: dict[str, _ScopeValue] | None = None,
    ) -> None:
        """初始化模块、事件枚举、收集模式和最终模块绑定。"""
        self._module_name = module_name
        self._event_members = event_members
        self._collect_facts = collect_facts
        self._module_final_scope = module_final_scope or {}
        self._scopes: list[dict[str, _ScopeValue]] = [{}]
        self._scope_kinds = ["module"]
        self._function_final_scopes: list[dict[str, _ScopeValue]] = []
        self._qualnames: list[str] = []
        self.static: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.dynamic: list[dict[str, Any]] = []

    def module_scope(self) -> dict[str, _ScopeValue]:
        """返回按模块执行顺序收敛后的符号状态。"""
        return dict(self._scopes[0])

    def _lookup(self, name: str) -> _ScopeValue:
        """从内向外解析 lexical binding。"""
        skip_class_scope = self._scope_kinds[-1] == "comprehension"
        for scope, kind in reversed(list(zip(self._scopes, self._scope_kinds))):
            if skip_class_scope and kind == "class":
                continue
            if name in scope:
                return scope[name]
        return None

    def _set(self, name: str, value: _ScopeValue) -> None:
        """在当前 lexical scope 写入或清空 binding。"""
        self._scopes[-1][name] = value

    @staticmethod
    def _merge_scope_states(
        states: list[list[dict[str, _ScopeValue]]],
    ) -> list[dict[str, _ScopeValue]]:
        """仅保留所有控制流路径一致的静态 provenance。"""
        return [
            {
                name: (
                    scopes[0].get(name)
                    if all(
                        scope.get(name) == scopes[0].get(name)
                        for scope in scopes[1:]
                    )
                    else None
                )
                for name in set().union(*(scope.keys() for scope in scopes))
            }
            for scopes in zip(*states)
        ]

    def _discover_scope_after(
        self,
        statements: list[ast.stmt],
        scopes: list[dict[str, _ScopeValue]],
        scope_kinds: list[str],
    ) -> dict[str, _ScopeValue]:
        """无事实副作用地计算一组语句执行后的最内层 scope。"""
        discovery = _EventConsumerCollector(
            self._module_name,
            self._event_members,
            collect_facts=False,
            module_final_scope=self._module_final_scope,
        )
        discovery._scopes = [dict(scope) for scope in scopes]
        discovery._scope_kinds = list(scope_kinds)
        for statement in statements:
            discovery.visit(statement)
        return dict(discovery._scopes[-1])

    def _symbol_for_canonical(self, canonical: str) -> _ScopeValue:
        """把有限 canonical 路径转换为 collector symbol。"""
        if canonical == "app.runtime.events.eventmanager":
            return _Symbol("manager_instance")
        if canonical == "app.runtime.events.EventManager":
            return _Symbol("manager_class")
        if canonical == "typing.TYPE_CHECKING":
            return _Symbol("type_checking")
        if canonical in {
            "app",
            "app.runtime",
            "app.runtime.events",
            "app.schemas",
            "app.schemas.types",
            "typing",
        }:
            return _Symbol("module", canonical)
        for enum_name, members in self._event_members.items():
            if canonical == f"app.schemas.types.{enum_name}":
                return _EventSelection(
                    events=tuple(f"{enum_name}.{member}" for member in members),
                    kind="enum",
                )
        return None

    def _runtime_type_checking_value(self, test: ast.expr) -> bool | None:
        """仅对可证明的 typing.TYPE_CHECKING 返回确定运行期分支。"""
        negate = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        target = test.operand if negate else test
        resolved = self._resolve(target)
        if isinstance(resolved, _Symbol) and resolved.kind == "type_checking":
            return negate
        return None

    def _resolve(self, node: ast.AST) -> _ScopeValue:
        """解析有限 import/module/赋值别名，不跨函数推断返回值。"""
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            parent = self._resolve(node.value)
            if isinstance(parent, _Symbol) and parent.kind == "module":
                return self._symbol_for_canonical(f"{parent.value}.{node.attr}")
            if isinstance(parent, _Symbol) and parent.kind == "manager_class":
                if node.attr == "get_existing_instance":
                    return _Symbol("manager_factory")
            if isinstance(parent, _EventSelection) and parent.kind == "enum":
                enum_name = parent.events[0].split(".", 1)[0] if parent.events else ""
                if node.attr in self._event_members.get(enum_name, ()):
                    return _EventSelection(
                        events=(f"{enum_name}.{node.attr}",),
                        kind="member",
                    )
            return None
        if isinstance(node, ast.List):
            return self._resolve_event_selection(
                node,
                allow_enum=True,
                allow_list=True,
            )
        if isinstance(node, ast.Call):
            target = self._resolve(node.func)
            if isinstance(target, _Symbol) and target.kind in {
                "manager_class",
                "manager_factory",
            }:
                return _Symbol("manager_instance")
            registration = self._registration(node)
            if registration and registration.method == "register":
                return _DecoratorFactory(
                    selection=registration.selection,
                    priority=registration.priority,
                )
        return None

    def _resolve_event_selection(
        self,
        node: ast.AST,
        *,
        allow_enum: bool,
        allow_list: bool,
    ) -> _EventSelection:
        """解析单个事件、enum 类或 register 接受的静态 list。"""
        if isinstance(node, ast.List):
            if not allow_list:
                return _EventSelection((), "list", dynamic=True)
            selections = [
                self._resolve_event_selection(
                    item,
                    allow_enum=True,
                    allow_list=False,
                )
                for item in node.elts
            ]
            return _EventSelection(
                events=tuple(sorted({event for item in selections for event in item.events})),
                kind="list",
                dynamic=any(item.dynamic for item in selections),
            )
        if isinstance(node, ast.IfExp):
            branches = (
                self._resolve_event_selection(
                    node.body,
                    allow_enum=allow_enum,
                    allow_list=allow_list,
                ),
                self._resolve_event_selection(
                    node.orelse,
                    allow_enum=allow_enum,
                    allow_list=allow_list,
                ),
            )
            return _EventSelection(
                events=tuple(sorted({event for item in branches for event in item.events})),
                kind="list" if allow_list else "member",
                dynamic=any(item.dynamic for item in branches),
            )
        resolved = self._resolve(node)
        if isinstance(resolved, _EventSelection):
            if resolved.kind == "member":
                return resolved
            if resolved.kind == "enum" and allow_enum:
                return resolved
            if resolved.kind == "list" and allow_list:
                return resolved
        return _EventSelection((), "member", dynamic=True)

    @staticmethod
    def _bind_call_arguments(
        node: ast.Call,
        parameter_names: tuple[str, ...],
        required_names: frozenset[str],
    ) -> dict[str, ast.AST] | None:
        """按真实 Python 调用规则绑定有限签名，拒绝未知或重复参数。"""
        if len(node.args) > len(parameter_names) or any(
            isinstance(argument, ast.Starred) for argument in node.args
        ):
            return None
        arguments = dict(zip(parameter_names, node.args))
        for keyword in node.keywords:
            if (
                keyword.arg is None
                or keyword.arg not in parameter_names
                or keyword.arg in arguments
            ):
                return None
            arguments[keyword.arg] = keyword.value
        if not required_names.issubset(arguments):
            return None
        return arguments

    def _registration(self, node: ast.Call) -> _Registration | None:
        """仅解析 receiver 已证明为 canonical EventManager 实例的注册。"""
        if not isinstance(node.func, ast.Attribute):
            return None
        method = node.func.attr
        if method not in _EVENT_MANAGER_METHODS:
            return None
        receiver = self._resolve(node.func.value)
        if not isinstance(receiver, _Symbol) or receiver.kind != "manager_instance":
            return None
        if method == "register":
            arguments = self._bind_call_arguments(
                node,
                ("etype", "priority"),
                frozenset({"etype"}),
            )
            event_name = "etype"
            handler_node = None
        else:
            arguments = self._bind_call_arguments(
                node,
                ("event_type", "handler", "priority"),
                frozenset({"event_type", "handler"}),
            )
            event_name = "event_type"
            handler_node = arguments.get("handler") if arguments else None
        if arguments is None:
            return None
        selection = self._resolve_event_selection(
            arguments[event_name],
            allow_enum=method == "register",
            allow_list=method == "register",
        )
        return _Registration(
            method=method,
            selection=selection,
            handler=_handler_identity(handler_node),
            priority=_priority_identity(arguments.get("priority")),
        )

    def _decorator_factory_application(
        self,
        node: ast.Call,
    ) -> tuple[_DecoratorFactory, ast.AST] | None:
        """解析合法的 decorator(f) 或 decorator(f=...) 立即应用。"""
        factory = self._resolve(node.func)
        if not isinstance(factory, _DecoratorFactory):
            return None
        arguments = self._bind_call_arguments(
            node,
            ("f",),
            frozenset({"f"}),
        )
        if arguments is None:
            return None
        return factory, arguments["f"]

    def _record(
        self,
        node: ast.AST,
        selection: _EventSelection,
        *,
        handler: str,
        priority: str,
        registration_kind: Literal["decorator", "listener"],
    ) -> None:
        """写入静态事件位置，并为未知余项写入一条 dynamic 位置。"""
        if not self._collect_facts:
            return
        location = {
            "caller": self._module_name,
            "line": node.lineno,
            "handler": handler,
            "priority": priority,
            "registration_kind": registration_kind,
            "identity": f"{registration_kind}|{handler}|{priority}",
        }
        for event in selection.events:
            self.static[event].append(dict(location))
        if selection.dynamic:
            self.dynamic.append(dict(location))

    def _record_decorator(self, decorator: ast.expr, handler: str) -> bool:
        """记录直接或简单赋值别名形式的 register decorator。"""
        factory = self._resolve(decorator)
        if not isinstance(factory, _DecoratorFactory):
            return False
        self._record(
            decorator,
            factory.selection,
            handler=handler,
            priority=factory.priority,
            registration_kind="decorator",
        )
        return True

    def _visit_definition_decorators(
        self,
        decorators: list[ast.expr],
        handler: str,
    ) -> None:
        """按定义期 scope 访问装饰器，Event register 只在实际应用时记账。"""
        for decorator in decorators:
            if not self._record_decorator(decorator, handler):
                self.visit(decorator)

    def visit_Import(self, node: ast.Import) -> None:
        """发布模块 import 及其别名。"""
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            canonical = alias.name if alias.asname else bound_name
            self._set(bound_name, self._symbol_for_canonical(canonical))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """发布 canonical from-import 及其别名。"""
        if node.level or not node.module:
            for alias in node.names:
                if alias.name != "*":
                    self._set(alias.asname or alias.name, None)
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            self._set(
                alias.asname or alias.name,
                self._symbol_for_canonical(f"{node.module}.{alias.name}"),
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        """按执行顺序传播或清空简单赋值别名。"""
        value = self._resolve(node.value)
        self.visit(node.value)
        for target in node.targets:
            for name in _bound_names(target):
                self._set(name, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """传播带注解且有值的简单赋值别名。"""
        value = self._resolve(node.value) if node.value is not None else None
        if node.value is not None:
            self.visit(node.value)
        for name in _bound_names(node.target):
            self._set(name, value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """增量赋值使目标 provenance 失效。"""
        self.visit(node.value)
        for name in _bound_names(node.target):
            self._set(name, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """传播海象表达式的简单别名。"""
        value = self._resolve(node.value)
        self.visit(node.value)
        scope_index = len(self._scopes) - 1
        while self._scope_kinds[scope_index] == "comprehension":
            scope_index -= 1
        for name in _bound_names(node.target):
            self._scopes[scope_index][name] = value

    def visit_Delete(self, node: ast.Delete) -> None:
        """删除名称后清空其 provenance。"""
        for target in node.targets:
            for name in _bound_names(target):
                self._set(name, None)

    def visit_If(self, node: ast.If) -> None:
        """精确执行 TYPE_CHECKING 分支，其余条件保守合并 binding。"""
        self.visit(node.test)
        runtime_value = self._runtime_type_checking_value(node.test)
        if runtime_value is not None:
            statements = node.body if runtime_value else node.orelse
            for statement in statements:
                self.visit(statement)
            return

        original = [dict(scope) for scope in self._scopes]
        for statement in node.body:
            self.visit(statement)
        body_scopes = [dict(scope) for scope in self._scopes]
        self._scopes = [dict(scope) for scope in original]
        for statement in node.orelse:
            self.visit(statement)
        else_scopes = [dict(scope) for scope in self._scopes]
        self._scopes = self._merge_scope_states([body_scopes, else_scopes])

    def visit_For(self, node: ast.For) -> None:
        """让循环目标遮蔽旧绑定，并保守合并零次与迭代路径。"""
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """按同步循环相同规则处理异步循环目标。"""
        self._visit_for(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        """实现同步和异步循环共享的 provenance 分析。"""
        self.visit(node.iter)
        original = [dict(scope) for scope in self._scopes]
        for name in _bound_names(node.target):
            self._set(name, None)
        for statement in node.body:
            self.visit(statement)
        body_scopes = [dict(scope) for scope in self._scopes]
        loop_exit = self._merge_scope_states([original, body_scopes])
        self._scopes = [dict(scope) for scope in loop_exit]
        for statement in node.orelse:
            self.visit(statement)
        else_scopes = [dict(scope) for scope in self._scopes]
        self._scopes = self._merge_scope_states([loop_exit, else_scopes])

    def visit_While(self, node: ast.While) -> None:
        """保守合并 while 的零次、迭代和 else 路径。"""
        self.visit(node.test)
        original = [dict(scope) for scope in self._scopes]
        for statement in node.body:
            self.visit(statement)
        body_scopes = [dict(scope) for scope in self._scopes]
        loop_exit = self._merge_scope_states([original, body_scopes])
        self._scopes = [dict(scope) for scope in loop_exit]
        for statement in node.orelse:
            self.visit(statement)
        else_scopes = [dict(scope) for scope in self._scopes]
        self._scopes = self._merge_scope_states([loop_exit, else_scopes])

    def visit_With(self, node: ast.With) -> None:
        """按进入顺序分析 context，并清空 with 目标的旧 provenance。"""
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """按同步 with 相同规则处理异步上下文目标。"""
        self._visit_with(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        """实现同步和异步上下文管理器共享的绑定分析。"""
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                for name in _bound_names(item.optional_vars):
                    self._set(name, None)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        """隔离 match case 捕获名称并保守合并所有匹配路径。"""
        self.visit(node.subject)
        original = [dict(scope) for scope in self._scopes]
        states = [original]
        for case in node.cases:
            self._scopes = [dict(scope) for scope in original]
            self.visit(case.pattern)
            for name in _pattern_bound_names(case.pattern):
                self._set(name, None)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
            states.append([dict(scope) for scope in self._scopes])
        self._scopes = self._merge_scope_states(states)

    def visit_Try(self, node: ast.Try) -> None:
        """隔离异常处理路径，并让异常别名在 handler 内外失效。"""
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        """按普通 try 相同规则处理异常组分支。"""
        self._visit_try(node)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        """实现 try 与 try-star 共享的保守控制流合并。"""
        original = [dict(scope) for scope in self._scopes]
        exception_states = [original]
        for statement in node.body:
            exception_states.append([dict(scope) for scope in self._scopes])
            self.visit(statement)
            exception_states.append([dict(scope) for scope in self._scopes])
        exception_entry = self._merge_scope_states(exception_states)
        for statement in node.orelse:
            self.visit(statement)
        continuing_states = [[dict(scope) for scope in self._scopes]]
        for handler in node.handlers:
            self._scopes = [dict(scope) for scope in exception_entry]
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name:
                self._set(handler.name, None)
            for statement in handler.body:
                self.visit(statement)
            if handler.name:
                self._set(handler.name, None)
            continuing_states.append([dict(scope) for scope in self._scopes])
        self._scopes = self._merge_scope_states(
            [*continuing_states, exception_entry]
            if node.finalbody
            else continuing_states
        )
        for statement in node.finalbody:
            self.visit(statement)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        expressions: tuple[ast.expr, ...],
    ) -> None:
        """在独立 lexical scope 内分析推导式目标和表达式。"""
        first, *remaining = generators
        self.visit(first.iter)
        outer_original = [dict(scope) for scope in self._scopes]
        self._scopes.append({})
        self._scope_kinds.append("comprehension")
        for name in _bound_names(first.target):
            self._set(name, None)
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator.iter)
            for name in _bound_names(generator.target):
                self._set(name, None)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)
        self._scope_kinds.pop()
        self._scopes.pop()
        outer_after = [dict(scope) for scope in self._scopes]
        self._scopes = self._merge_scope_states([outer_original, outer_after])

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """在独立 scope 内分析列表推导式。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """在独立 scope 内分析集合推导式。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """在独立 scope 内分析生成器表达式。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """在独立 scope 内分析字典推导式。"""
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """按定义期装饰器和调用期 lexical scope 分别分析函数。"""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """按普通函数相同规则分析异步函数。"""
        self._visit_function(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        """实现同步和异步函数共享的 scope 分析。"""
        if not self._collect_facts:
            self._set(node.name, None)
            return
        handler = ".".join((*self._qualnames, node.name))
        self._visit_definition_decorators(node.decorator_list, handler)
        for expression in (
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ):
            self.visit(expression)

        saved_scopes = self._scopes
        saved_kinds = self._scope_kinds
        body_scopes = [
            dict(self._module_final_scope),
            *(dict(scope) for scope in self._function_final_scopes),
            {name: None for name in _function_local_names(node)},
        ]
        body_kinds = [
            "module",
            *("function" for _ in self._function_final_scopes),
            "function",
        ]
        final_scope = self._discover_scope_after(node.body, body_scopes, body_kinds)
        self._scopes = body_scopes
        self._scope_kinds = body_kinds
        self._function_final_scopes.append(final_scope)
        self._qualnames.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._qualnames.pop()
        self._function_final_scopes.pop()
        self._scopes = saved_scopes
        self._scope_kinds = saved_kinds
        self._set(node.name, None)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """在类定义期 scope 记录类装饰器和方法装饰器。"""
        if not self._collect_facts:
            self._set(node.name, None)
            return
        handler = ".".join((*self._qualnames, node.name))
        self._visit_definition_decorators(node.decorator_list, handler)
        for expression in (*node.bases, *(keyword.value for keyword in node.keywords)):
            self.visit(expression)
        self._qualnames.append(node.name)
        self._scopes.append({})
        self._scope_kinds.append("class")
        for statement in node.body:
            self.visit(statement)
        self._scope_kinds.pop()
        self._scopes.pop()
        self._qualnames.pop()
        self._set(node.name, None)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """让 lambda 参数遮蔽同名模块 alias。"""
        if not self._collect_facts:
            return
        for expression in (
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ):
            self.visit(expression)
        saved_scopes = self._scopes
        saved_kinds = self._scope_kinds
        body_scopes = [
            dict(self._module_final_scope),
            *(dict(scope) for scope in self._function_final_scopes),
            {name: None for name in _function_local_names(node)},
        ]
        body_kinds = [
            "module",
            *("function" for _ in self._function_final_scopes),
            "function",
        ]
        final_scope = self._discover_scope_after(
            [ast.Expr(value=node.body)],
            body_scopes,
            body_kinds,
        )
        self._scopes = body_scopes
        self._scope_kinds = body_kinds
        self._function_final_scopes.append(final_scope)
        self.visit(node.body)
        self._function_final_scopes.pop()
        self._scopes = saved_scopes
        self._scope_kinds = saved_kinds

    def visit_Call(self, node: ast.Call) -> None:
        """记录直接 listener 调用或立即应用的 register decorator。"""
        factory_application = self._decorator_factory_application(node)
        if factory_application:
            factory, handler_node = factory_application
            self._record(
                node,
                factory.selection,
                handler=_handler_identity(handler_node),
                priority=factory.priority,
                registration_kind="decorator",
            )
        else:
            registration = self._registration(node)
            if not registration or registration.method != "add_event_listener":
                self.generic_visit(node)
                return
            self._record(
                node,
                registration.selection,
                handler=registration.handler,
                priority=registration.priority,
                registration_kind="listener",
            )
        self.generic_visit(node)


def collect_event_consumers(
    modules: dict[str, Path],
    event_members: dict[str, tuple[str, ...]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """
    收集宿主 EventManager 的静态和动态 consumer 位置

    只有可追溯到 ``app.runtime.events.eventmanager`` 或 canonical
    ``EventManager`` 实例的注册才进入事实；未知 receiver 直接忽略。

    :param modules: 宿主模块名到 Python 源码路径的映射
    :param event_members: EventType/ChainEventType 到公开成员名的映射
    :return: 静态事件位置映射与事件值未知的位置列表
    """
    static: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dynamic: list[dict[str, Any]] = []
    for module_name, path in sorted(modules.items()):
        if module_name == "app.plugins" or module_name.startswith("app.plugins."):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        discovery = _EventConsumerCollector(
            module_name,
            event_members,
            collect_facts=False,
        )
        discovery.visit(tree)
        collector = _EventConsumerCollector(
            module_name,
            event_members,
            collect_facts=True,
            module_final_scope=discovery.module_scope(),
        )
        collector.visit(tree)
        for event, locations in collector.static.items():
            static[event].extend(locations)
        dynamic.extend(collector.dynamic)

    return (
        {
            event: sorted(locations, key=_location_sort_key)
            for event, locations in sorted(static.items())
        },
        sorted(dynamic, key=_location_sort_key),
    )
