"""收集宿主直接网络传输、网络 SDK 与底层协议操作事实。"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

RAW_TRANSPORT_ROOTS = (
    "requests",
    "httpx",
    "httpx2",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "http.client",
    "websocket",
    "websockets",
    "curl_cffi",
    "cloudscraper",
    "httplib2",
    "pycurl",
)
NETWORK_SDK_ROOTS = (
    "boto3",
    "botocore",
    "cloakbrowser",
    "ddgs",
    "discord",
    "docker",
    "google.genai",
    "langchain_anthropic",
    "langchain_aws",
    "langchain_deepseek",
    "langchain_google_genai",
    "langchain_openai",
    "lark_oapi",
    "openai",
    "oss2",
    "plexapi",
    "pywebpush",
    "qbittorrentapi",
    "redis",
    "slack_bolt",
    "slack_sdk",
    "smbclient",
    "smbprotocol",
    "telebot",
    "transmission_rpc",
)
PROTOCOL_OPERATIONS = {
    "asyncio.open_connection",
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "xmlrpc.client.ServerProxy",
}
REEXPORTED_TARGETS = {
    ("app.adapters.network.http", "requests"): "requests",
}


@dataclass(frozen=True)
class _SymbolBinding:
    """记录表达式符号对应的导入来源与网络能力。"""

    canonical: str
    target: str | None
    binding: str


_SymbolChoices = tuple[_SymbolBinding, ...]


def _unique_bindings(bindings: Iterable[_SymbolBinding]) -> _SymbolChoices:
    """把多个可证明来源去重并稳定排序。"""
    return tuple(
        sorted(
            set(bindings),
            key=lambda item: (item.target or "", item.canonical, item.binding),
        )
    )


def _is_type_checking_test(test: ast.expr) -> bool:
    """判断条件是否只在静态类型检查阶段成立。"""
    return (
        isinstance(test, ast.Name)
        and test.id == "TYPE_CHECKING"
    ) or (
        isinstance(test, ast.Attribute)
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
        and test.attr == "TYPE_CHECKING"
    )


def _runtime_import_nodes(tree: ast.AST) -> Iterable[ast.Import | ast.ImportFrom]:
    """遍历运行期导入并排除 TYPE_CHECKING 分支。"""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        child: ast.AST = node
        parent = parents.get(child)
        while parent is not None:
            if (
                isinstance(parent, ast.If)
                and _is_type_checking_test(parent.test)
                and child in parent.body
            ):
                break
            child = parent
            parent = parents.get(parent)
        else:
            yield node


def _registered_target(imported: str) -> tuple[str, str] | None:
    """把导入模块归一到最长的 raw transport 或 network SDK 根。"""
    candidates = sorted(
        (*RAW_TRANSPORT_ROOTS, *NETWORK_SDK_ROOTS),
        key=len,
        reverse=True,
    )
    for target in candidates:
        if imported == target or imported.startswith(f"{target}."):
            kind = "raw_transport" if target in RAW_TRANSPORT_ROOTS else "network_sdk"
            return target, kind
    return None


def _attribute_parts(node: ast.AST) -> tuple[str, ...] | None:
    """把 Name/Attribute 表达式拆成稳定的点分片段。"""
    if isinstance(node, ast.Name):
        return (node.id,)
    if not isinstance(node, ast.Attribute):
        return None
    prefix = _attribute_parts(node.value)
    if prefix is None:
        return None
    return (*prefix, node.attr)


def _bound_name(node: ast.AST) -> tuple[str, ...] | None:
    """返回可进行有限传播的 Name 或 self/cls 属性名。"""
    parts = _attribute_parts(node)
    if not parts:
        return None
    if len(parts) == 1 or parts[0] in {"self", "cls"}:
        return parts
    return None


def _fact_fingerprint(fact: dict[str, object]) -> str:
    """为不含行号的完整事实生成稳定指纹。"""
    payload = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _returns_network_capability(target: str, kind: str, operation: str) -> bool:
    """判断调用结果是否仍是应继续追踪的 client/capability。"""
    name = operation.rsplit(".", maxsplit=1)[-1]
    if kind == "raw_transport":
        return name in {
            "AsyncClient",
            "Client",
            "ClientSession",
            "Session",
            "WebSocketApp",
            "__call__",
        }
    return (
        name[:1].isupper()
        or name in {"client", "connect", "register_session", "resource"}
        or target in {"smbclient"}
    )


class _EgressVisitor(ast.NodeVisitor):
    """根据 import provenance 收集稳定的网络能力使用点。"""

    def __init__(
        self,
        source: str,
        import_records: dict[ast.AST, dict[tuple[str, ...], _SymbolChoices]],
        facts: dict[tuple[str, str, str], dict[str, object]],
        *,
        function_injections: dict[
            ast.FunctionDef | ast.AsyncFunctionDef,
            dict[str, _SymbolChoices],
        ] | None = None,
        module_final_scope: dict[
            tuple[str, ...], _SymbolChoices | None
        ] | None = None,
    ) -> None:
        """初始化单模块 visitor，并复用预收集的导入符号。"""
        self._source = source
        self._import_records = import_records
        self._facts = facts
        self._qualnames: list[str] = []
        self._scopes: list[dict[tuple[str, ...], _SymbolChoices | None]] = [{}]
        self._scope_kinds = ["module"]
        self._call_results: dict[ast.Call, _SymbolChoices | None] = {}
        self._function_injections = {
            function: dict(injections)
            for function, injections in (function_injections or {}).items()
        }
        self._functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self._module_final_scope = (
            dict(module_final_scope) if module_final_scope is not None else None
        )
        self._collecting_class_bindings = False

    def _qualname(self) -> str:
        """返回当前调用所在的稳定限定名。"""
        return ".".join(self._qualnames) or "<module>"

    def _fact(self, target: str, kind: str) -> dict[str, object]:
        """取得或创建当前 source/capability 的聚合事实。"""
        key = (self._source, target, kind)
        return self._facts.setdefault(
            key,
            {
                "source": self._source,
                "target": target,
                "kind": kind,
                "bindings": set(),
                "uses": [],
            },
        )

    def _resolve(self, node: ast.AST) -> tuple[_SymbolChoices, tuple[str, ...]] | None:
        """按最长符号前缀解析表达式及剩余操作片段。"""
        parts = _attribute_parts(node)
        if not parts:
            return None
        for scope in reversed(self._scopes):
            for size in range(len(parts), 0, -1):
                key = parts[:size]
                if key not in scope:
                    continue
                binding = scope[key]
                if binding is None:
                    return None
                return binding, parts[size:]
        return None

    def _set_symbol(
        self,
        name: tuple[str, ...],
        binding: _SymbolChoices | None,
    ) -> None:
        """写入当前 lexical scope；self/cls 属性写入最近类作用域。"""
        if name[0] in {"self", "cls"}:
            for index in range(len(self._scope_kinds) - 1, -1, -1):
                if self._scope_kinds[index] == "class":
                    if self._collecting_class_bindings:
                        if binding is not None:
                            existing = self._scopes[index].get(name) or ()
                            self._scopes[index][name] = _unique_bindings(
                                (*existing, *binding)
                            )
                        return
                    self._scopes[index][name] = binding
                    return
        self._scopes[-1][name] = binding

    def _push_scope(self, kind: str) -> None:
        """压入一个 lexical symbol scope。"""
        self._scopes.append({})
        self._scope_kinds.append(kind)

    def _pop_scope(self) -> None:
        """弹出当前 lexical symbol scope。"""
        self._scopes.pop()
        self._scope_kinds.pop()

    def seed_module(self, tree: ast.Module) -> None:
        """预发布模块级 import/alias，使函数分析不依赖定义先后。"""
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def is_module_level(node: ast.AST) -> bool:
            parent = parents.get(node)
            while parent is not None and parent is not tree:
                if isinstance(
                    parent,
                    (
                        ast.ClassDef,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.Lambda,
                        ast.ListComp,
                        ast.SetComp,
                        ast.DictComp,
                        ast.GeneratorExp,
                    ),
                ):
                    return False
                parent = parents.get(parent)
            return True

        for import_node, symbols in self._import_records.items():
            if is_module_level(import_node):
                self._scopes[0].update(symbols)
        for node in ast.walk(tree):
            if not is_module_level(node):
                continue
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue
            choices = self._static_bindings(value)
            if not choices:
                continue
            for target_node in targets:
                target = _bound_name(target_node)
                if target and len(target) == 1:
                    self._scopes[0][target] = choices
        self._functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def function_injections(self) -> dict[
        ast.FunctionDef | ast.AsyncFunctionDef,
        dict[str, _SymbolChoices],
    ]:
        """返回当前调用点已证明的顶层函数参数来源。"""
        return {
            function: dict(injections)
            for function, injections in self._function_injections.items()
        }

    def module_scope(self) -> dict[tuple[str, ...], _SymbolChoices | None]:
        """返回按模块执行顺序收敛后的符号状态。"""
        return dict(self._scopes[0])

    def _record_function_injection(self, node: ast.Call) -> None:
        """从调用方当前 scope 传播顶层包装函数的可证明参数来源。"""
        if not isinstance(node.func, ast.Name):
            return
        function = self._functions.get(node.func.id)
        if function is None:
            return
        positional = (*function.args.posonlyargs, *function.args.args)
        arguments: list[tuple[str, ast.AST]] = [
            (parameter.arg, argument)
            for parameter, argument in zip(positional, node.args)
        ]
        known_parameters = {
            argument.arg
            for argument in (*positional, *function.args.kwonlyargs)
        }
        arguments.extend(
            (keyword.arg, keyword.value)
            for keyword in node.keywords
            if keyword.arg in known_parameters
        )
        injections = self._function_injections.setdefault(function, {})
        for parameter, argument in arguments:
            choices = self._static_bindings(argument)
            if not choices:
                continue
            injections[parameter] = _unique_bindings(
                (*injections.get(parameter, ()), *choices)
            )

    def _shadow_arguments(self, arguments: ast.arguments) -> None:
        """让函数参数遮蔽同名外层 import 或别名。"""
        all_arguments = (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
        for argument in all_arguments:
            if argument.arg in {"self", "cls"}:
                continue
            self._set_symbol(
                (argument.arg,),
                self._annotation_bindings(argument.annotation),
            )
        if arguments.vararg:
            self._set_symbol((arguments.vararg.arg,), None)
        if arguments.kwarg:
            self._set_symbol((arguments.kwarg.arg,), None)

    def _annotation_bindings(self, annotation: ast.AST | None) -> _SymbolChoices | None:
        """从显式 Client/Session 参数注解恢复注入网络能力。"""
        if annotation is None:
            return None
        bindings: list[_SymbolBinding] = []
        for candidate in ast.walk(annotation):
            if not isinstance(candidate, (ast.Name, ast.Attribute)):
                continue
            resolved = self._resolve(candidate)
            if not resolved:
                continue
            choices, remainder = resolved
            for binding in choices:
                canonical = ".".join(
                    part for part in (binding.canonical, *remainder) if part
                )
                if canonical.rsplit(".", maxsplit=1)[-1] not in {
                    "AsyncClient",
                    "Client",
                    "ClientSession",
                    "Session",
                }:
                    continue
                bindings.append(
                    _SymbolBinding(
                        canonical=binding.target or canonical,
                        target=binding.target,
                        binding=binding.binding,
                    )
                )
        return _unique_bindings(bindings) or None

    def _static_bindings(self, value: ast.AST) -> _SymbolChoices | None:
        """不记录 use，仅解析赋值表达式可能产生的网络能力集合。"""
        if isinstance(value, ast.Call):
            resolved = self._resolve(value.func)
        elif isinstance(value, ast.IfExp):
            return _unique_bindings(
                binding
                for branch in (value.body, value.orelse)
                for binding in (self._static_bindings(branch) or ())
            ) or None
        else:
            resolved = self._resolve(value)
        if not resolved:
            return None
        choices, remainder = resolved
        if isinstance(value, ast.Call):
            choices = tuple(
                binding
                for binding in choices
                if binding.target is not None
                and (registered := _registered_target(binding.target)) is not None
                and _returns_network_capability(
                    registered[0],
                    registered[1],
                    ".".join(
                        part for part in (binding.canonical, *remainder) if part
                    ).removeprefix(registered[0]).lstrip(".")
                    or "__call__",
                )
            )
        return _unique_bindings(
            _SymbolBinding(
                canonical=binding.target
                or ".".join(
                    part for part in (binding.canonical, *remainder) if part
                ),
                target=binding.target,
                binding=binding.binding,
            )
            for binding in choices
            if binding.target is not None
        ) or None

    def _record_call(self, node: ast.Call) -> _SymbolChoices | None:
        """记录已知能力调用或精确底层协议操作。"""
        if node in self._call_results:
            return self._call_results[node]
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (registered := _registered_target(node.args[0].value))
        ):
            target, kind = registered
            binding_text = f"dynamic:__import__({node.args[0].value})"
            fact = self._fact(target, kind)
            fact["bindings"].add(binding_text)  # type: ignore[union-attr]
            fact["uses"].append(f"{self._qualname()}|call:dynamic-import")  # type: ignore[union-attr]
            result = (
                _SymbolBinding(
                    canonical=target,
                    target=target,
                    binding=binding_text,
                ),
            )
            self._call_results[node] = result
            return result
        resolved = self._resolve(node.func)
        if (
            resolved is None
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
        ):
            if parent_bindings := self._record_call(node.func.value):
                resolved = (parent_bindings, (node.func.attr,))
        if resolved is None:
            self._call_results[node] = None
            return None
        bindings, remainder = resolved
        results: list[_SymbolBinding] = []
        for binding in bindings:
            canonical = ".".join(
                part for part in (binding.canonical, *remainder) if part
            )
            if (
                canonical == "importlib.import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (registered := _registered_target(node.args[0].value))
            ):
                target, kind = registered
                binding_text = f"dynamic:importlib.import_module({node.args[0].value})"
                fact = self._fact(target, kind)
                fact["bindings"].add(binding_text)  # type: ignore[union-attr]
                fact["uses"].append(f"{self._qualname()}|call:dynamic-import")  # type: ignore[union-attr]
                results.append(
                    _SymbolBinding(
                        canonical=target,
                        target=target,
                        binding=binding_text,
                    )
                )
                continue
            if canonical in PROTOCOL_OPERATIONS:
                fact = self._fact(canonical, "protocol_operation")
                fact["bindings"].add(binding.binding)  # type: ignore[union-attr]
                fact["uses"].append(f"{self._qualname()}|call:{canonical}")  # type: ignore[union-attr]
                results.append(
                    _SymbolBinding(
                        canonical=canonical,
                        target=canonical,
                        binding=binding.binding,
                    )
                )
                continue
            if binding.target is None:
                continue
            registered = _registered_target(binding.target)
            if registered is None:
                continue
            target, kind = registered
            operation = canonical.removeprefix(target).lstrip(".") or "__call__"
            fact = self._fact(target, kind)
            fact["bindings"].add(binding.binding)  # type: ignore[union-attr]
            fact["uses"].append(f"{self._qualname()}|call:{operation}")  # type: ignore[union-attr]
            if _returns_network_capability(target, kind, operation):
                results.append(
                    _SymbolBinding(
                        canonical=target,
                        target=target,
                        binding=binding.binding,
                    )
                )
        result = _unique_bindings(results) or None
        self._call_results[node] = result
        return result

    def _propagate(self, target_node: ast.AST, value: ast.AST) -> None:
        """把简单别名或 Client/Session 获取结果传播到局部符号。"""
        target_name = _bound_name(target_node)
        if target_name is None:
            return
        source_bindings: _SymbolChoices | None = None
        if isinstance(value, ast.Call):
            source_bindings = self._record_call(value)
        elif isinstance(value, ast.IfExp):
            alternatives = [self._resolve(value.body), self._resolve(value.orelse)]
            resolved_bindings = [binding for item in alternatives if item for binding in item[0]]
            source_bindings = _unique_bindings(
                _SymbolBinding(
                    canonical=binding.target or binding.canonical,
                    target=binding.target,
                    binding=binding.binding,
                )
                for binding in resolved_bindings
            ) or None
        else:
            resolved = self._resolve(value)
            if resolved:
                bindings, remainder = resolved
                source_bindings = _unique_bindings(
                    _SymbolBinding(
                        canonical=".".join(
                            part for part in (binding.canonical, *remainder) if part
                        ),
                        target=binding.target,
                        binding=binding.binding,
                    )
                    for binding in bindings
                )
        self._set_symbol(target_name, source_bindings)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """在类限定名下访问定义体。"""
        for expression in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(expression.value if isinstance(expression, ast.keyword) else expression)
        self._qualnames.append(node.name)
        self._push_scope("class")
        self._prebind_class_attributes(node)
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()
        self._qualnames.pop()

    def _prebind_class_attributes(self, node: ast.ClassDef) -> None:
        """无副作用预分析各方法，累积 self/cls 可证明网络能力。"""
        original_facts = self._facts
        original_results = self._call_results
        original_flag = self._collecting_class_bindings
        self._facts = {}
        self._call_results = {}
        self._collecting_class_bindings = True
        try:
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.visit(method)
        finally:
            self._collecting_class_bindings = original_flag
            self._call_results = original_results
            self._facts = original_facts

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """在函数限定名下访问定义体。"""
        for expression in (
            *node.decorator_list,
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ):
            self.visit(expression)
        original_module_scope = self._scopes[0]
        if self._module_final_scope is not None:
            self._scopes[0] = dict(self._module_final_scope)
        class_index = next(
            (
                index
                for index in range(len(self._scope_kinds) - 1, -1, -1)
                if self._scope_kinds[index] == "class"
            ),
            None,
        )
        class_scope = (
            dict(self._scopes[class_index])
            if class_index is not None
            else None
        )
        self._qualnames.append(node.name)
        self._push_scope("function")
        self._shadow_arguments(node.args)
        for parameter, choices in self._function_injections.get(node, {}).items():
            existing = self._scopes[-1].get((parameter,)) or ()
            self._set_symbol(
                (parameter,),
                _unique_bindings((*existing, *choices)),
            )
        positional = (*node.args.posonlyargs, *node.args.args)
        for argument, default in zip(
            positional[-len(node.args.defaults):],
            node.args.defaults,
        ):
            if self._static_bindings(default):
                self._propagate(ast.Name(id=argument.arg), default)
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None and self._static_bindings(default):
                self._propagate(ast.Name(id=argument.arg), default)
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()
        self._qualnames.pop()
        if (
            not self._collecting_class_bindings
            and class_index is not None
            and class_scope is not None
        ):
            self._scopes[class_index] = class_scope
        self._scopes[0] = original_module_scope

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """在异步函数限定名下访问定义体。"""
        self.visit_FunctionDef(node)

    def visit_Import(self, node: ast.Import) -> None:
        """在导入发生的 lexical scope 发布网络符号。"""
        self._scopes[-1].update(self._import_records.get(node, {}))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """在 from-import 发生的 lexical scope 发布网络符号。"""
        self._scopes[-1].update(self._import_records.get(node, {}))

    def visit_If(self, node: ast.If) -> None:
        """跳过 TYPE_CHECKING 专用分支，其余条件按源码顺序扫描。"""
        self.visit(node.test)
        if _is_type_checking_test(node.test):
            for statement in node.orelse:
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

        merged_scopes: list[dict[tuple[str, ...], _SymbolChoices | None]] = []
        for original_scope, body_scope, else_scope in zip(
            original,
            body_scopes,
            else_scopes,
        ):
            merged: dict[tuple[str, ...], _SymbolChoices | None] = {}
            for name in original_scope.keys() | body_scope.keys() | else_scope.keys():
                alternatives = _unique_bindings(
                    binding
                    for binding in (
                        body_scope.get(name, original_scope.get(name)),
                        else_scope.get(name, original_scope.get(name)),
                    )
                    if binding is not None
                    for binding in binding
                )
                merged[name] = alternatives or None
            merged_scopes.append(merged)
        self._scopes = merged_scopes

    def visit_Try(self, node: ast.Try) -> None:
        """按成功/异常路径合并符号来源，finally 在合并结果上执行。"""
        original = [dict(scope) for scope in self._scopes]
        states: list[list[dict[tuple[str, ...], _SymbolChoices | None]]] = []

        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        states.append([dict(scope) for scope in self._scopes])

        for handler in node.handlers:
            self._scopes = [dict(scope) for scope in original]
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name:
                self._set_symbol((handler.name,), None)
            for statement in handler.body:
                self.visit(statement)
            states.append([dict(scope) for scope in self._scopes])

        merged_scopes: list[dict[tuple[str, ...], _SymbolChoices | None]] = []
        for index, original_scope in enumerate(original):
            merged: dict[tuple[str, ...], _SymbolChoices | None] = {}
            names = original_scope.keys()
            for state in states:
                names = names | state[index].keys()
            for name in names:
                alternatives = _unique_bindings(
                    binding
                    for state in states
                    for choices in (state[index].get(name, original_scope.get(name)),)
                    if choices is not None
                    for binding in choices
                )
                merged[name] = alternatives or None
            merged_scopes.append(merged)
        self._scopes = merged_scopes
        for statement in node.finalbody:
            self.visit(statement)

    def visit_For(self, node: ast.For) -> None:
        """for 目标在循环体遮蔽同名符号，并保留循环零次路径。"""
        self.visit(node.iter)
        original = [dict(scope) for scope in self._scopes]
        target = _bound_name(node.target)
        if target:
            self._set_symbol(target, None)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        body_scopes = [dict(scope) for scope in self._scopes]
        merged_scopes: list[dict[tuple[str, ...], _SymbolChoices | None]] = []
        for original_scope, body_scope in zip(original, body_scopes):
            merged: dict[tuple[str, ...], _SymbolChoices | None] = {}
            for name in original_scope.keys() | body_scope.keys():
                alternatives = _unique_bindings(
                    binding
                    for choices in (
                        original_scope.get(name),
                        body_scope.get(name, original_scope.get(name)),
                    )
                    if choices is not None
                    for binding in choices
                )
                merged[name] = alternatives or None
            merged_scopes.append(merged)
        self._scopes = merged_scopes

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """异步 for 使用与同步 for 相同的遮蔽规则。"""
        self.visit_For(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """lambda 参数只在其表达式 scope 内遮蔽外层符号。"""
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self._push_scope("lambda")
        self._shadow_arguments(node.args)
        self.visit(node.body)
        self._pop_scope()

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        expressions: tuple[ast.AST, ...],
    ) -> None:
        """在独立 comprehension scope 中处理迭代目标遮蔽。"""
        self._push_scope("comprehension")
        for generator in generators:
            self.visit(generator.iter)
            target = _bound_name(generator.target)
            if target:
                self._set_symbol(target, None)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)
        self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """访问列表推导式的独立 scope。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """访问集合推导式的独立 scope。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """访问生成器表达式的独立 scope。"""
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """访问字典推导式的独立 scope。"""
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_Assign(self, node: ast.Assign) -> None:
        """记录赋值右侧调用并传播简单目标。"""
        self.visit(node.value)
        for target in node.targets:
            self._propagate(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """记录带注解赋值并传播简单目标。"""
        if node.value is not None:
            self.visit(node.value)
            self._propagate(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """记录海象赋值并传播简单目标。"""
        self.visit(node.value)
        self._propagate(node.target, node.value)

    def visit_With(self, node: ast.With) -> None:
        """记录上下文能力获取，并把 as 目标传播到定义体。"""
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._propagate(item.optional_vars, item.context_expr)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """记录异步上下文能力获取，并把 as 目标传播到定义体。"""
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._propagate(item.optional_vars, item.context_expr)
        for statement in node.body:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:
        """记录调用后继续访问参数中的嵌套调用。"""
        self._record_call(node)
        self._record_function_injection(node)
        self.visit(node.func)
        for argument in (*node.args, *node.keywords):
            self.visit(argument.value if isinstance(argument, ast.keyword) else argument)


def _import_records(
    source: str,
    tree: ast.Module,
    facts: dict[tuple[str, str, str], dict[str, object]],
) -> dict[ast.AST, dict[tuple[str, ...], _SymbolChoices]]:
    """预收集运行期 import 事实，并按 AST 节点保留 lexical 发布记录。"""
    records: dict[ast.AST, dict[tuple[str, ...], _SymbolChoices]] = {}
    for node in _runtime_import_nodes(tree):
        symbols: dict[tuple[str, ...], _SymbolChoices] = {}
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
                canonical = alias.name if alias.asname else bound
                registered = _registered_target(alias.name)
                target, kind = registered if registered else (None, None)
                binding_text = f"import:{alias.name} as {bound}"
                symbols[(bound,)] = (
                    _SymbolBinding(canonical, target, binding_text),
                )
                if registered:
                    fact = facts.setdefault(
                        (source, target, kind),
                        {
                            "source": source,
                            "target": target,
                            "kind": kind,
                            "bindings": set(),
                            "uses": [],
                        },
                    )
                    fact["bindings"].add(binding_text)  # type: ignore[union-attr]
            records[node] = symbols
            continue
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                if registered := _registered_target(module):
                    target, kind = registered
                    binding_text = f"from:{module}.* as *"
                    fact = facts.setdefault(
                        (source, target, kind),
                        {
                            "source": source,
                            "target": target,
                            "kind": kind,
                            "bindings": set(),
                            "uses": [],
                        },
                    )
                    fact["bindings"].add(binding_text)  # type: ignore[union-attr]
                continue
            bound = alias.asname or alias.name
            reexport = REEXPORTED_TARGETS.get((module, alias.name))
            imported = reexport or f"{module}.{alias.name}"
            registered = _registered_target(imported) or _registered_target(module)
            target, kind = registered if registered else (None, None)
            if reexport:
                canonical = reexport
                binding_text = f"reexport:{module}.{alias.name} as {bound}"
            elif registered and imported == target:
                canonical = imported
                binding_text = f"from:{module}.{alias.name} as {bound}"
            else:
                canonical = imported
                binding_text = f"from:{module}.{alias.name} as {bound}"
            symbols[(bound,)] = (
                _SymbolBinding(canonical, target, binding_text),
            )
            if registered:
                fact = facts.setdefault(
                    (source, target, kind),
                    {
                        "source": source,
                        "target": target,
                        "kind": kind,
                        "bindings": set(),
                        "uses": [],
                    },
                )
                fact["bindings"].add(binding_text)  # type: ignore[union-attr]
        records[node] = symbols
    return records


def collect_direct_egress(modules: dict[str, Path]) -> dict[str, object]:
    """收集宿主 direct egress 事实、注册表和稳定统计。"""
    facts: dict[tuple[str, str, str], dict[str, object]] = {}
    for source, path in sorted(modules.items()):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        discovery_facts: dict[tuple[str, str, str], dict[str, object]] = {}
        discovery_records = _import_records(source, tree, discovery_facts)
        discovery = _EgressVisitor(source, discovery_records, discovery_facts)
        discovery.seed_module(tree)
        discovery.visit(tree)
        import_records = _import_records(source, tree, facts)
        visitor = _EgressVisitor(
            source,
            import_records,
            facts,
            function_injections=discovery.function_injections(),
            module_final_scope=discovery.module_scope(),
        )
        visitor.seed_module(tree)
        visitor.visit(tree)

    entries: list[dict[str, object]] = []
    for fact in facts.values():
        normalized = {
            "source": fact["source"],
            "target": fact["target"],
            "kind": fact["kind"],
            "bindings": sorted(fact["bindings"]),
            "uses": sorted(fact["uses"]),
        }
        entries.append({**normalized, "fingerprint": _fact_fingerprint(normalized)})
    entries.sort(key=lambda item: (item["source"], item["target"], item["kind"]))

    counts_by_kind = {
        kind: sum(entry["kind"] == kind for entry in entries)
        for kind in ("raw_transport", "network_sdk", "protocol_operation")
    }
    source_roots = {
        root: sum(
            entry["source"] == root or str(entry["source"]).startswith(f"{root}.")
            for entry in entries
        )
        for root in ("app.application", "app.chain")
    }
    return {
        "scope": {
            "root": "app",
            "excluded_roots": ["app.plugins"],
            "runtime_only": True,
            "line_numbers": False,
        },
        "registry": {
            "raw_transports": list(RAW_TRANSPORT_ROOTS),
            "network_sdks": list(NETWORK_SDK_ROOTS),
            "protocol_operations": sorted(PROTOCOL_OPERATIONS),
            "reexports": [
                {
                    "source": f"{module}.{member}",
                    "target": target,
                }
                for (module, member), target in sorted(REEXPORTED_TARGETS.items())
            ],
        },
        "count": len(entries),
        "counts_by_kind": counts_by_kind,
        "application_chain_counts": source_roots,
        "entries": entries,
    }
