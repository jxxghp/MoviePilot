#!/usr/bin/env python3
"""生成并校验 MoviePilot 后端架构与插件兼容契约基线。"""

import argparse
import ast
import dataclasses
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

try:
    from scripts.architecture.egress import collect_direct_egress
except ModuleNotFoundError:
    from egress import collect_direct_egress

try:
    from scripts.architecture.event_facts import collect_event_facts
except ModuleNotFoundError:
    from event_facts import collect_event_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"
BASELINE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "architecture"
DEPENDENCY_BASELINE_PATH = BASELINE_ROOT / "dependency-baseline.json"
DEPENDENCY_POLICY_PATH = BASELINE_ROOT / "dependency-policy.json"
RUNTIME_BASELINE_PATH = BASELINE_ROOT / "runtime-contract-baseline.json"
TRANSACTION_BASELINE_PATH = BASELINE_ROOT / "transaction-debt-baseline.json"
CONFIGURATION_BASELINE_PATH = BASELINE_ROOT / "configuration-debt-baseline.json"
PLUGIN_BASELINE_PATH = BASELINE_ROOT / "official-plugin-baseline.json"
PLUGIN_HOOKS = (
    "get_actions",
    "get_agent_tools",
    "get_api",
    "get_auth_provider",
    "get_command",
    "get_dashboard",
    "get_form",
    "get_module",
    "get_page",
    "get_render_mode",
    "get_service",
    "get_sidebar",
    "get_state",
    "init_plugin",
    "stop_service",
)
MODEL_TRANSACTION_DECORATORS = {
    "async_db_query",
    "async_db_update",
    "db_query",
    "db_update",
}
SESSION_FACTORY_NAMES = {
    "AsyncSession",
    "AsyncSessionFactory",
    "ScopedSession",
    "Session",
    "SessionFactory",
    "async_session_scope",
    "get_async_db",
    "get_async_session_factory",
    "get_db",
    "get_scoped_session",
    "get_session_factory",
}

CONFIGURATION_EXCLUDED_ROOTS = (
    APP_ROOT / "plugins",
    APP_ROOT / "sdk",
    APP_ROOT / "runtime" / "compat",
    APP_ROOT / "testing",
)
FOUNDATIONAL_SETTINGS_BOUNDARIES = {
    "app/db/base.py": "模型声明阶段必须在运行时配置服务装配前确定数据库主键类型",
    "app/db/engine.py": "数据库引擎是运行时配置服务的底层依赖，不能通过兼容代理自递归",
    "app/db/session.py": "数据库会话与连接配额必须在应用组合根装配前可用",
}
COMPOSITION_ROOT_OPER_BOUNDARIES = {
    ("app/startup/initializers/modules.py", "SystemConfigOper"):
        "启动组合根负责构造唯一的系统配置数据库适配器",
}


def discover_modules() -> dict[str, Path]:
    """返回宿主 Python 模块与源码路径，排除运行时插件副本。"""
    modules: dict[str, Path] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[:2] == ["app", "plugins"]:
            continue
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def parse_source(path: Path) -> ast.Module:
    """以仓库统一编码解析 Python 源码。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


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


def iter_runtime_import_nodes(tree: ast.AST):
    """遍历运行期导入，排除 ``if TYPE_CHECKING`` 内的仅类型依赖。"""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.If) and _is_type_checking_test(parent.test):
                break
            parent = parents.get(parent)
        else:
            yield node


def collect_configuration_debt_baseline() -> dict[str, Any]:
    """分离配置债务与数据库基础设施、组合根的固定批准边界。"""
    settings_files: list[str] = []
    foundational_settings: list[dict[str, str]] = []
    oper_calls: list[dict[str, Any]] = []
    composition_root_calls: list[dict[str, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if any(path.is_relative_to(root) for root in CONFIGURATION_EXCLUDED_ROOTS):
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = parse_source(path)
        direct_oper_names: set[str] = set()
        imports_settings = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "app.runtime.config" and any(
                alias.name == "settings" and alias.asname in (None, "settings")
                for alias in node.names
            ):
                imports_settings = True
            if node.module == "app.db.oper.systemconfig":
                direct_oper_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "SystemConfigOper"
                )
        if imports_settings:
            if reason := FOUNDATIONAL_SETTINGS_BOUNDARIES.get(relative):
                foundational_settings.append({"file": relative, "reason": reason})
            else:
                settings_files.append(relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in direct_oper_names:
                call = {"file": relative, "name": node.func.id}
                boundary_key = (relative, node.func.id)
                if reason := COMPOSITION_ROOT_OPER_BOUNDARIES.get(boundary_key):
                    composition_root_calls.append({**call, "reason": reason})
                else:
                    oper_calls.append(call)
    return {
        "schema_version": 2,
        "scope": {
            "root": "app",
            "excluded": [
                "app/plugins",
                "app/sdk",
                "app/runtime/compat",
                "app/testing",
            ],
        },
        "settings_imports": {
            "count": len(settings_files),
            "files": settings_files,
        },
        "foundational_settings_boundaries": {
            "count": len(foundational_settings),
            "entries": foundational_settings,
        },
        "system_config_oper_constructions": {
            "count": len(oper_calls),
            "calls": oper_calls,
        },
        "composition_root_oper_boundaries": {
            "count": len(composition_root_calls),
            "entries": composition_root_calls,
        },
    }


def iter_import_candidates(
    module_name: str,
    path: Path,
) -> list[tuple[str, Optional[str]]]:
    """提取模块导入候选，第二项记录 from-import 的具体符号。"""
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    candidates: list[tuple[str, Optional[str]]] = []
    for node in iter_runtime_import_nodes(parse_source(path)):
        if isinstance(node, ast.Import):
            candidates.extend((alias.name, None) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".")
            base = ".".join(package_parts[: len(package_parts) - node.level + 1])
            imported_module = ".".join(
                part for part in (base, node.module or "") if part
            )
        else:
            imported_module = node.module or ""
        if not imported_module:
            continue
        candidates.extend(
            (imported_module, alias.name)
            for alias in node.names
            if alias.name != "*"
        )
    return candidates


def resolve_imports(
    module_name: str,
    path: Path,
    known_modules: set[str],
) -> set[str]:
    """解析宿主内部静态导入，并计入 Python 必然初始化的父包。"""
    dependencies: set[str] = set()
    for imported_module, imported_name in iter_import_candidates(module_name, path):
        candidates = [imported_module]
        if imported_name:
            candidates.append(f"{imported_module}.{imported_name}")
        for candidate in candidates:
            parts = candidate.split(".")
            dependencies.update(
                parent
                for index in range(2, len(parts))
                if (parent := ".".join(parts[:index])) in known_modules
            )
            if candidate in known_modules:
                dependencies.add(candidate)
    dependencies.discard(module_name)
    return dependencies


def _is_module_or_child(module_name: str, root: str) -> bool:
    """判断模块是否等于指定根或位于其点分子树内。"""
    return module_name == root or module_name.startswith(f"{root}.")


def _resolve_import_from_module(
    module_name: str,
    path: Path,
    node: ast.ImportFrom,
) -> str:
    """把 from-import 的相对模块解析为绝对模块名，非法越顶时返回空串。"""
    if not node.level:
        return node.module or ""
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    package_parts = package.split(".") if package else []
    keep_count = len(package_parts) - node.level + 1
    if keep_count < 0:
        return ""
    base = ".".join(package_parts[:keep_count])
    return ".".join(part for part in (base, node.module or "") if part)


def collect_direct_adapter_imports(
    modules: dict[str, Path],
) -> list[dict[str, str]]:
    """收集 Application/Chain 对 Adapter 的原始运行期 import，不展开父包。"""
    source_roots = ("app.application", "app.chain")
    target_root = "app.adapters"
    edges: set[tuple[str, str]] = set()
    for source, path in modules.items():
        if not any(_is_module_or_child(source, root) for root in source_roots):
            continue
        for node in iter_runtime_import_nodes(parse_source(path)):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_module = _resolve_import_from_module(source, path, node)
                if imported_module:
                    targets.append(imported_module)
                    targets.extend(
                        f"{imported_module}.{alias.name}"
                        for alias in node.names
                        if imported_module == "app" and alias.name != "*"
                    )
            edges.update(
                (source, target)
                for target in targets
                if _is_module_or_child(target, target_root)
            )
    return [
        {"source": source, "target": target}
        for source, target in sorted(edges)
    ]


def strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[list[str]]:
    """使用 Tarjan 算法返回稳定排序的非平凡强连通分量。"""
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(module_name: str) -> None:
        """深度遍历模块并在根节点收集强连通分量。"""
        indices[module_name] = len(indices)
        low_links[module_name] = indices[module_name]
        stack.append(module_name)
        on_stack.add(module_name)
        for dependency in sorted(graph[module_name]):
            if dependency not in indices:
                visit(dependency)
                low_links[module_name] = min(
                    low_links[module_name], low_links[dependency]
                )
            elif dependency in on_stack:
                low_links[module_name] = min(
                    low_links[module_name], indices[dependency]
                )
        if low_links[module_name] != indices[module_name]:
            return
        component: list[str] = []
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module_name:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for module_name in sorted(graph):
        if module_name not in indices:
            visit(module_name)
    return sorted(components)


def collect_boundary_edges(
    graph: dict[str, set[str]],
    modules: dict[str, Path],
) -> dict[str, list[str]]:
    """收集治理文档指定的当前越层边，供后续阶段逐项收缩。"""
    boundaries: dict[str, list[str]] = {
        "adapters_to_db": [],
        "agent_to_db": [],
        "api_to_db": [],
        "api_endpoints_to_db_models": [],
        "api_endpoints_to_sessions": [],
        "application_to_agent": [],
        "application_to_db": [],
        "chain_to_db": [],
        "modules_to_db": [],
        "monitor_to_db": [],
        "runtime_to_db": [],
        "workflow_to_db": [],
    }
    for source, dependencies in graph.items():
        for target in dependencies:
            edge = f"{source} -> {target}"
            if source.startswith("app.adapters") and target.startswith("app.db"):
                boundaries["adapters_to_db"].append(edge)
            if source.startswith("app.agent") and target.startswith("app.db"):
                boundaries["agent_to_db"].append(edge)
            if source.startswith("app.api") and target.startswith("app.db"):
                boundaries["api_to_db"].append(edge)
            if source.startswith("app.application") and target.startswith("app.db"):
                boundaries["application_to_db"].append(edge)
            if source.startswith("app.chain") and target.startswith("app.db"):
                boundaries["chain_to_db"].append(edge)
            if source.startswith("app.modules") and target.startswith("app.db"):
                boundaries["modules_to_db"].append(edge)
            if source.startswith("app.monitor") and target.startswith("app.db"):
                boundaries["monitor_to_db"].append(edge)
            if source.startswith("app.runtime") and target.startswith("app.db"):
                boundaries["runtime_to_db"].append(edge)
            if source.startswith("app.workflow") and target.startswith("app.db"):
                boundaries["workflow_to_db"].append(edge)
            if source.startswith("app.api.endpoints") and target.startswith(
                "app.db.models"
            ):
                boundaries["api_endpoints_to_db_models"].append(edge)
            if source.startswith("app.application") and target.startswith("app.agent"):
                boundaries["application_to_agent"].append(edge)
    for source, path in modules.items():
        if not source.startswith("app.api.endpoints"):
            continue
        for imported_module, imported_name in iter_import_candidates(source, path):
            if imported_module not in {
                "sqlalchemy.orm",
                "sqlalchemy.ext.asyncio",
            }:
                continue
            if imported_name not in {"Session", "AsyncSession"}:
                continue
            boundaries["api_endpoints_to_sessions"].append(
                f"{source} -> {imported_module}.{imported_name}"
            )
    return {
        boundary: sorted(set(edges))
        for boundary, edges in sorted(boundaries.items())
    }


def collect_dependency_baseline() -> dict[str, Any]:
    """生成宿主模块、依赖边、SCC 和越层边的完整基线。"""
    modules = discover_modules()
    known_modules = set(modules)
    graph = {
        name: resolve_imports(name, path, known_modules)
        for name, path in modules.items()
    }
    edges = sorted(
        f"{source} -> {target}"
        for source, dependencies in graph.items()
        for target in dependencies
    )
    digest = hashlib.sha256("\n".join(edges).encode("utf-8")).hexdigest()
    direct_adapter_imports = collect_direct_adapter_imports(modules)
    direct_adapter_sources = sorted(
        {edge["source"] for edge in direct_adapter_imports}
    )
    direct_adapter_targets = sorted(
        {edge["target"] for edge in direct_adapter_imports}
    )
    return {
        "schema_version": 3,
        "scope": "MoviePilot host app excluding app/plugins",
        "module_count": len(modules),
        "edge_count": len(edges),
        "edge_sha256": digest,
        "modules": sorted(modules),
        "edges": edges,
        "strongly_connected_components": strongly_connected_components(graph),
        "direct_adapter_imports": {
            "scope": {
                "source_roots": ["app.application", "app.chain"],
                "target_root": "app.adapters",
                "runtime_only": True,
                "parent_package_expansion": False,
                "imported_symbols": False,
            },
            "count": len(direct_adapter_imports),
            "counts_by_source_root": {
                "app.application": sum(
                    _is_module_or_child(edge["source"], "app.application")
                    for edge in direct_adapter_imports
                ),
                "app.chain": sum(
                    _is_module_or_child(edge["source"], "app.chain")
                    for edge in direct_adapter_imports
                ),
            },
            "source_count": len(direct_adapter_sources),
            "sources": direct_adapter_sources,
            "target_count": len(direct_adapter_targets),
            "targets": direct_adapter_targets,
            "edges": direct_adapter_imports,
        },
        "direct_egress": collect_direct_egress(modules),
        "boundary_edges": collect_boundary_edges(graph, modules),
    }


def _expression_name(node: ast.AST) -> str:
    """返回调用或装饰器表达式的点分名称，无法静态解析时返回空串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return ".".join(part for part in (prefix, node.attr) if part)
    if isinstance(node, ast.Call):
        return _expression_name(node.func)
    return ""


def _iter_owned_functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """收集模块函数与类方法的稳定限定名，不记录易漂移源码行号。"""
    methods: list[tuple[str, ast.AST]] = []

    def visit_class(node: ast.ClassDef, parents: tuple[str, ...]) -> None:
        """递归访问嵌套类，并收集直接定义的方法。"""
        class_path = (*parents, node.name)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append((".".join((*class_path, child.name)), child))
            elif isinstance(child, ast.ClassDef):
                visit_class(child, class_path)

    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            visit_class(statement, ())
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append((statement.name, statement))
    return methods


def _collect_method_calls(
    root: Path,
    *,
    operations: set[str],
) -> list[dict[str, str]]:
    """按文件、方法和操作收集指定调用，作为只降不增的事务债务清单。"""
    calls: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for method, node in _iter_owned_functions(parse_source(path)):
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                expression = _expression_name(call.func)
                operation = expression.rsplit(".", 1)[-1]
                if operation in operations:
                    calls.append(
                        {
                            "file": relative,
                            "method": method,
                            "operation": operation,
                        }
                    )
    return sorted(
        calls,
        key=lambda item: (item["file"], item["method"], item["operation"]),
    )


def collect_transaction_debt_baseline() -> dict[str, Any]:
    """记录 Model 自动事务和 Oper 会话所有权债务，供 CI 执行单向 ratchet。"""
    model_root = APP_ROOT / "db" / "models"
    oper_root = APP_ROOT / "db" / "oper"
    decorated_methods: list[dict[str, str]] = []
    for path in sorted(model_root.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for method, node in _iter_owned_functions(parse_source(path)):
            for decorator in node.decorator_list:
                decorator_name = _expression_name(decorator).rsplit(".", 1)[-1]
                if decorator_name in MODEL_TRANSACTION_DECORATORS:
                    decorated_methods.append(
                        {
                            "decorator": decorator_name,
                            "file": relative,
                            "method": method,
                        }
                    )
    decorated_methods.sort(
        key=lambda item: (item["file"], item["method"], item["decorator"])
    )
    model_transaction_calls = _collect_method_calls(
        model_root,
        operations={"commit", "rollback"},
    )
    model_session_factories = _collect_method_calls(
        model_root,
        operations=SESSION_FACTORY_NAMES,
    )
    oper_transaction_calls = _collect_method_calls(
        oper_root,
        operations={"commit", "rollback"},
    )
    oper_session_factories = _collect_method_calls(
        oper_root,
        operations=SESSION_FACTORY_NAMES,
    )
    decorator_counts = {
        decorator: sum(
            item["decorator"] == decorator
            for item in decorated_methods
        )
        for decorator in sorted(MODEL_TRANSACTION_DECORATORS)
    }
    return {
        "schema_version": 1,
        "scope": "app/db/models and app/db/oper transaction ownership debt",
        "model_decorators": {
            "count": len(decorated_methods),
            "by_kind": decorator_counts,
            "methods": decorated_methods,
        },
        "model_transaction_calls": {
            "count": len(model_transaction_calls),
            "calls": model_transaction_calls,
        },
        "model_session_factories": {
            "count": len(model_session_factories),
            "calls": model_session_factories,
        },
        "oper_transaction_calls": {
            "count": len(oper_transaction_calls),
            "calls": oper_transaction_calls,
        },
        "oper_session_factories": {
            "count": len(oper_session_factories),
            "calls": oper_session_factories,
        },
    }
def _collect_run_module_locations() -> tuple[
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
]:
    """扫描模块调度调用及其当前源码位置。"""
    calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dynamic_calls: list[dict[str, Any]] = []
    for module_name, path in discover_modules().items():
        tree = parse_source(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"run_module", "async_run_module"}:
                continue
            location = {
                "caller": module_name,
                "line": node.lineno,
                "mode": "async" if node.func.attr == "async_run_module" else "sync",
            }
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                calls[node.args[0].value].append(location)
            else:
                dynamic_calls.append(location)
    return calls, dynamic_calls


def _aggregate_locations(
    locations: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """按稳定字段聚合调用位置，排除行号但保留调用次数。"""
    counts: dict[tuple[Any, ...], int] = defaultdict(int)
    for location in locations:
        counts[tuple(location[field] for field in fields)] += 1
    return [
        {
            **dict(zip(fields, values)),
            "count": count,
        }
        for values, count in sorted(counts.items())
    ]


def collect_run_module_contracts() -> dict[str, Any]:
    """收集不受源码行号变化影响的模块调度语义契约。"""
    calls, dynamic_calls = _collect_run_module_locations()
    stable_calls = {
        method: _aggregate_locations(locations, ("caller", "mode"))
        for method, locations in sorted(calls.items())
    }
    return {
        "method_count": len(stable_calls),
        "call_count": sum(len(locations) for locations in calls.values()),
        "dynamic_call_count": len(dynamic_calls),
        "methods": stable_calls,
        "dynamic_calls": _aggregate_locations(dynamic_calls, ("caller", "mode")),
    }


def collect_run_module_diagnostics() -> dict[str, Any]:
    """收集模块调度调用的当前源码位置，仅用于人工诊断。"""
    calls, dynamic_calls = _collect_run_module_locations()
    return {
        "methods": {
            method: sorted(
                locations,
                key=lambda item: (item["caller"], item["line"], item["mode"]),
            )
            for method, locations in sorted(calls.items())
        },
        "dynamic_calls": sorted(
            dynamic_calls,
            key=lambda item: (item["caller"], item["line"], item["mode"]),
        ),
    }


def _event_enum_members(enum_name: str) -> tuple[str, ...]:
    """从 schema 源码读取事件枚举成员，避免基线脚本导入宿主运行时。"""
    tree = parse_source(APP_ROOT / "schemas" / "types.py")
    enum_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == enum_name
        ),
        None,
    )
    if enum_class is None:
        raise RuntimeError(f"未找到事件枚举：{enum_name}")
    return tuple(
        target.id
        for statement in enum_class.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name) and not target.id.startswith("_")
    )


def collect_current_event_facts() -> dict[str, list[dict[str, Any]]]:
    """收集排除插件副本的当前宿主 Event producer/consumer 事实。"""
    event_members = _event_enum_members("EventType")
    chain_event_members = _event_enum_members("ChainEventType")
    return collect_event_facts(
        discover_modules(),
        {
            "EventType": event_members,
            "ChainEventType": chain_event_members,
        },
    )


def _line_free_event_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """移除只用于诊断的源码行号，保留完整稳定事件身份。"""
    return {key: value for key, value in fact.items() if key != "line"}


def collect_event_fact_contract() -> dict[str, Any]:
    """生成逐调用事实与按 fingerprint 索引的宿主事件契约。"""
    event_names = sorted([
        *(
            f"EventType.{member}"
            for member in _event_enum_members("EventType")
        ),
        *(
            f"ChainEventType.{member}"
            for member in _event_enum_members("ChainEventType")
        ),
    ])
    current = collect_current_event_facts()
    producers = sorted(
        (_line_free_event_fact(fact) for fact in current["producers"]),
        key=lambda fact: (fact["caller"], fact["qualname"], fact["fingerprint"]),
    )
    consumers = sorted(
        (_line_free_event_fact(fact) for fact in current["consumers"]),
        key=lambda fact: (fact["caller"], fact["qualname"], fact["fingerprint"]),
    )
    all_facts = (*producers, *consumers)
    event_index = {
        event_name: {
            "producer_fingerprints": sorted(
                fact["fingerprint"]
                for fact in producers
                if event_name in fact["events"]
            ),
            "consumer_fingerprints": sorted(
                fact["fingerprint"]
                for fact in consumers
                if event_name in fact["events"]
            ),
        }
        for event_name in event_names
    }
    return {
        "event_count": len(event_names),
        "producer_call_count": len(producers),
        "static_producer_call_count": sum(
            not fact["dynamic"] and not fact["invalid"] for fact in producers
        ),
        "dynamic_producer_count": sum(fact["dynamic"] for fact in producers),
        "invalid_producer_count": sum(fact["invalid"] for fact in producers),
        "producer_event_reference_count": sum(
            len(fact["events"]) for fact in producers
        ),
        "consumer_registration_count": len(consumers),
        "static_consumer_count": sum(
            not fact["dynamic"] and not fact["invalid"] for fact in consumers
        ),
        "dynamic_consumer_count": sum(fact["dynamic"] for fact in consumers),
        "invalid_consumer_count": sum(fact["invalid"] for fact in consumers),
        "consumer_event_reference_count": sum(
            len(fact["events"]) for fact in consumers
        ),
        "fact_count": len(all_facts),
        "producers": producers,
        "consumers": consumers,
        "event_index": event_index,
    }


def collect_event_fact_diagnostics() -> dict[str, list[dict[str, Any]]]:
    """返回带源码行号的逐调用事件事实，仅用于人工诊断。"""
    return collect_current_event_facts()


def _sdk_all_names(tree: ast.Module, path: Path) -> tuple[str, ...]:
    """读取 SDK 模块显式声明的 ``__all__``，未声明时不推断公开合同。"""
    value_node: Optional[ast.expr] = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value_node = node.value
    if value_node is None:
        return ()
    try:
        names = ast.literal_eval(value_node)
    except (ValueError, TypeError) as err:
        raise ValueError(f"SDK 模块 {path} 的 __all__ 必须是字符串列表") from err
    if not isinstance(names, (list, tuple)) or not all(
        isinstance(name, str) for name in names
    ):
        raise ValueError(f"SDK 模块 {path} 的 __all__ 必须是字符串列表")
    if len(names) != len(set(names)):
        raise ValueError(f"SDK 模块 {path} 的 __all__ 存在重复名称")
    return tuple(names)


def _sdk_alias_target(
    value: ast.expr,
    imported_targets: dict[str, str],
) -> str:
    """把顶层别名赋值解析为稳定目标，保留其真实 canonical 来源。"""
    if isinstance(value, ast.Name):
        return imported_targets.get(value.id, value.id)
    if isinstance(value, ast.Attribute):
        return f"{_sdk_alias_target(value.value, imported_targets)}.{value.attr}"
    return ast.unparse(value)


def _collect_sdk_module_exports(path: Path) -> list[dict[str, str]]:
    """按单个 SDK 模块的显式 ``__all__`` 生成可比较的符号合同。"""
    tree = parse_source(path)
    export_names = _sdk_all_names(tree, path)
    imported_targets: dict[str, str] = {}
    bindings: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_name = f"{'.' * node.level}{node.module}"
            for alias in node.names:
                if alias.name == "*":
                    continue
                public_name = alias.asname or alias.name
                target = f"{module_name}.{alias.name}"
                imported_targets[public_name] = target
                bindings[public_name] = {
                    "name": public_name,
                    "kind": "import",
                    "target": target,
                }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                public_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                imported_targets[public_name] = alias.name
                bindings[public_name] = {
                    "name": public_name,
                    "kind": "import",
                    "target": alias.name,
                }
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = {
                "name": node.name,
                "kind": type(node).__name__,
                "target": "",
            }
        elif isinstance(node, ast.Assign):
            for target_node in node.targets:
                if not isinstance(target_node, ast.Name) or target_node.id == "__all__":
                    continue
                bindings[target_node.id] = {
                    "name": target_node.id,
                    "kind": "alias",
                    "target": _sdk_alias_target(node.value, imported_targets),
                }
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id != "__all__"
            and node.value is not None
        ):
            bindings[node.target.id] = {
                "name": node.target.id,
                "kind": "alias",
                "target": _sdk_alias_target(node.value, imported_targets),
            }

    unresolved = sorted(set(export_names) - set(bindings))
    if unresolved:
        raise ValueError(
            f"SDK 模块 {path} 的 __all__ 含无法解析的顶层名称：{', '.join(unresolved)}"
        )
    return sorted(
        (bindings[name] for name in export_names),
        key=lambda item: (item["name"], item["kind"], item["target"]),
    )


def collect_sdk_exports() -> dict[str, list[dict[str, str]]]:
    """按显式 ``__all__`` 收集 SDK 合同，避免导入时物化运行资源。"""
    result: dict[str, list[dict[str, str]]] = {}
    for path in sorted((APP_ROOT / "sdk").glob("*.py")):
        module_name = f"app.sdk.{path.stem}" if path.stem != "__init__" else "app.sdk"
        result[module_name] = _collect_sdk_module_exports(path)
    return result


def json_compatible(value: Any) -> Any:
    """把兼容清单中的 dataclass、集合和映射转换为稳定 JSON 数据。"""
    if dataclasses.is_dataclass(value):
        return {
            field.name: json_compatible(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): json_compatible(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [json_compatible(item) for item in value]
        try:
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
        except TypeError:
            return items
    return value


def collect_compat_manifest() -> dict[str, Any]:
    """加载仅依赖标准库的兼容清单并序列化公开映射。"""
    path = APP_ROOT / "runtime" / "compat" / "manifest.py"
    spec = importlib.util.spec_from_file_location("architecture_compat_manifest", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载兼容清单：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    names = (
        "MODULE_ALIASES",
        "PACKAGE_ALIASES",
        "PACKAGE_EXPORTS",
        "SYMBOL_ALIASES",
        "VIRTUAL_PACKAGES",
    )
    return {
        name.lower(): json_compatible(getattr(module, name))
        for name in names
    }


def collect_runtime_baseline() -> dict[str, Any]:
    """生成模块调度、SDK 和兼容层公开契约基线。"""
    return {
        "schema_version": 3,
        "scope": {
            "repository": "MoviePilot",
            "roots": ["app"],
            "excluded": ["app/plugins"],
        },
        "run_module": collect_run_module_contracts(),
        "module_method_specs": collect_module_method_specs(),
        "event_facts": collect_event_fact_contract(),
        "event_specs": collect_event_specs(),
        "sdk_exports": collect_sdk_exports(),
        "compat_manifest": collect_compat_manifest(),
    }


def collect_module_method_specs() -> dict[str, Any]:
    """加载无运行资源副作用的 Module Contract V2 清单。"""
    path = APP_ROOT / "runtime" / "extensions" / "module" / "contracts.py"
    spec = importlib.util.spec_from_file_location("architecture_module_contracts", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块契约清单：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        contracts = module.list_explicit_module_contracts()
        return json_compatible(contracts)
    finally:
        sys.modules.pop(spec.name, None)


def collect_event_specs() -> dict[str, Any]:
    """收集全部 enum 事件的稳定 payload、可见性与可靠性登记。"""
    project_root = str(PROJECT_ROOT)
    inserted = project_root not in sys.path
    if inserted:
        sys.path.insert(0, project_root)
    try:
        from app.runtime.event.contracts import EVENT_CONTRACTS
    finally:
        if inserted:
            sys.path.remove(project_root)

    return {
        contract.event_name: {
            "payload_contract": contract.payload_contract,
            "input_contract": (
                contract.input_model.__name__ if contract.input_model else None
            ),
            "output_contract": (
                contract.output_model.__name__ if contract.output_model else None
            ),
            "schema_version": contract.schema_version,
            "payload_mode": contract.payload_mode.value,
            "validation_mode": contract.validation_mode.value,
            "mode": contract.mode,
            "visibility": contract.visibility.value,
            "delivery": contract.delivery.value,
            "error_behavior": contract.error_behavior.value,
            "ordering": contract.ordering,
            "sensitive_fields": list(contract.sensitive_fields),
            "legacy_reason": contract.legacy_reason,
        }
        for contract in EVENT_CONTRACTS.values()
    }


def collect_runtime_diagnostics() -> dict[str, Any]:
    """生成带当前源码行号的运行契约诊断视图，不写入语义 fixture。"""
    return {
        "run_module": collect_run_module_diagnostics(),
        "event_facts": collect_event_fact_diagnostics(),
    }


def git_head(repository: Path) -> str:
    """读取外部插件仓当前提交，失败时返回可诊断占位值。"""
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def collect_plugin_imports(path: Path) -> set[str]:
    """收集单个插件文件直接声明的 app 导入模块。"""
    imports: set[str] = set()
    for node in ast.walk(parse_source(path)):
        if isinstance(node, ast.Import):
            imports.update(
                alias.name for alias in node.names if alias.name.startswith("app.")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app."):
                imports.add(node.module)
    return imports


def collect_plugin_api_contracts(path: Path) -> list[dict[str, Any]]:
    """收集插件 ``get_api`` 中可静态解析的路由与响应模型声明。"""
    tree = parse_source(path)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    routes: list[dict[str, Any]] = []
    for function in functions.values():
        if function.name != "get_api":
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Dict):
                continue
            values = {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            path_node = values.get("path")
            if not isinstance(path_node, ast.Constant) or not isinstance(
                path_node.value, str
            ):
                continue
            endpoint_node = values.get("endpoint")
            endpoint = (
                endpoint_node.attr
                if isinstance(endpoint_node, ast.Attribute)
                else ast.unparse(endpoint_node) if endpoint_node else ""
            )
            endpoint_function = functions.get(endpoint)
            methods_node = values.get("methods")
            try:
                methods = ast.literal_eval(methods_node) if methods_node else []
            except (TypeError, ValueError):
                methods = [ast.unparse(methods_node)] if methods_node else []
            routes.append(
                {
                    "auth": ast.unparse(values["auth"]) if "auth" in values else None,
                    "endpoint": endpoint,
                    "endpoint_return": (
                        ast.unparse(endpoint_function.returns)
                        if endpoint_function and endpoint_function.returns
                        else None
                    ),
                    "methods": methods,
                    "path": path_node.value,
                    "response_class": (
                        ast.unparse(values["response_class"])
                        if "response_class" in values
                        else None
                    ),
                    "response_model": (
                        ast.unparse(values["response_model"])
                        if "response_model" in values
                        else None
                    ),
                }
            )
    return sorted(routes, key=lambda item: (item["path"], item["endpoint"]))


def _read_plugin_index(path: Path) -> dict[str, Any]:
    """读取插件索引；缺失索引按该代没有候选处理。"""
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"插件索引必须是对象：{path}")
    return value


def _v3_default_plugin_roots(plugin_repo: Path) -> dict[str, Path]:
    """返回 V3/V2 专用索引均未接管时可回退的默认插件源码目录。"""
    default_index = _read_plugin_index(plugin_repo / "package.json")
    v2_index = _read_plugin_index(plugin_repo / "package.v2.json")
    v3_index = _read_plugin_index(plugin_repo / "package.v3.json")
    roots: dict[str, Path] = {}
    for plugin_id, plugin_info in default_index.items():
        if not isinstance(plugin_info, dict):
            continue
        if plugin_info.get("v2") is not True or plugin_info.get("v3") is False:
            continue
        if plugin_id in v3_index:
            continue
        v2_info = v2_index.get(plugin_id)
        if isinstance(v2_info, dict) and v2_info.get("v3") is not False:
            continue
        plugin_root = plugin_repo / "plugins" / plugin_id.lower()
        if not plugin_root.is_dir():
            raise FileNotFoundError(f"V3 默认兼容插件缺少源码目录：{plugin_root}")
        roots[plugin_id] = plugin_root
    return roots


def collect_official_plugin_baseline(plugin_repo: Path) -> dict[str, Any]:
    """扫描独立官方插件仓中 V3 可见实现的导入面、Hook 和动态 API 契约。"""
    versioned_roots = [plugin_repo / "plugins.v2", plugin_repo / "plugins.v3"]
    default_roots = _v3_default_plugin_roots(plugin_repo)
    paths = sorted(
        {
            path
            for root in [*versioned_roots, *default_roots.values()]
            if root.exists()
            for path in root.rglob("*.py")
        }
    )
    import_files: dict[str, set[str]] = defaultdict(set)
    hook_files: dict[str, set[str]] = defaultdict(set)
    api_contracts: dict[str, list[dict[str, Any]]] = {}
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(plugin_repo).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        for imported_module in collect_plugin_imports(path):
            import_files[imported_module].add(relative)
        routes = collect_plugin_api_contracts(path)
        if routes:
            api_contracts[relative] = routes
        tree = ast.parse(content.decode("utf-8-sig"), filename=str(path))
        defined_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for hook in PLUGIN_HOOKS:
            if hook in defined_names:
                hook_files[hook].add(relative)
    return {
        "schema_version": 3,
        "scope": {
            "repository": "MoviePilot-Plugins",
            "roots": [*[root.name for root in versioned_roots], "plugins"],
            "default_plugins": sorted(default_roots),
        },
        "provenance": {
            "head": git_head(plugin_repo),
            "python_file_count": len(paths),
            "source_sha256": digest.hexdigest(),
        },
        "imports": {
            module: {
                "file_count": len(files),
                "files": sorted(files),
            }
            for module, files in sorted(import_files.items())
        },
        "hooks": {
            hook: {
                "file_count": len(hook_files.get(hook, set())),
                "files": sorted(hook_files.get(hook, set())),
            }
            for hook in PLUGIN_HOOKS
        },
        "api_routes": dict(sorted(api_contracts.items())),
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    """以稳定格式写入生成基线。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _display_path(path: Path) -> Path:
    """优先返回仓库相对路径，便于 CLI 输出稳定可读。"""
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def _migrate_runtime_v1_to_v2(value: dict[str, Any]) -> dict[str, Any]:
    """把包含源码行号的 v1 运行契约转换为 v2 聚合语义。"""
    run_module = value["run_module"]
    events = value["events"]
    return {
        "schema_version": 2,
        "run_module": {
            **{
                key: run_module[key]
                for key in ("method_count", "call_count", "dynamic_call_count")
            },
            "methods": {
                method: _aggregate_locations(locations, ("caller", "mode"))
                for method, locations in run_module["methods"].items()
            },
            "dynamic_calls": _aggregate_locations(
                run_module["dynamic_calls"],
                ("caller", "mode"),
            ),
        },
        "events": {
            **{
                key: events[key]
                for key in ("event_count", "producer_count", "consumer_count")
            },
            "events": {
                event_name: {
                    "producers": _aggregate_locations(
                        contract["producers"],
                        ("caller",),
                    ),
                    "consumers": _aggregate_locations(
                        contract["consumers"],
                        ("caller",),
                    ),
                }
                for event_name, contract in events["events"].items()
            },
            "dynamic_producers": _aggregate_locations(
                events["dynamic_producers"],
                ("caller",),
            ),
            "dynamic_consumers": _aggregate_locations(
                events["dynamic_consumers"],
                ("caller",),
            ),
        },
        "sdk_exports": value["sdk_exports"],
        "compat_manifest": value["compat_manifest"],
    }


def _migrate_runtime_v2_to_v3(value: dict[str, Any]) -> dict[str, Any]:
    """把 v2 聚合事件投影为显式待刷新的 v3 兼容视图。"""
    return {
        "schema_version": 3,
        "scope": {
            "repository": "MoviePilot",
            "roots": ["app"],
            "excluded": ["app/plugins"],
        },
        "run_module": value["run_module"],
        "module_method_specs": value.get("module_method_specs", {}),
        "event_facts": {
            "migration_required": True,
            "legacy_v2_projection": value["events"],
        },
        "event_specs": value.get("event_specs", {}),
        "sdk_exports": value["sdk_exports"],
        "compat_manifest": value["compat_manifest"],
    }


def _migrate_runtime_baseline(value: dict[str, Any]) -> dict[str, Any]:
    """链式迁移旧运行契约，保证检查只报告语义变化而不崩溃。"""
    migrated = value
    if migrated.get("schema_version") == 1:
        migrated = _migrate_runtime_v1_to_v2(migrated)
    if migrated.get("schema_version") == 2:
        migrated = _migrate_runtime_v2_to_v3(migrated)
    return migrated


def _migrate_plugin_baseline(value: dict[str, Any]) -> dict[str, Any]:
    """把来源信息混排的 v2 插件基线转换为 scope/provenance 结构。"""
    if value.get("schema_version") != 2:
        return value
    source = value["source"]
    return {
        "schema_version": 3,
        "scope": {
            "repository": source["repository"],
            "roots": source["roots"],
        },
        "provenance": {
            "head": source["head"],
            "python_file_count": source["python_file_count"],
            "source_sha256": source["source_sha256"],
        },
        "imports": value["imports"],
        "hooks": value["hooks"],
        "api_routes": value["api_routes"],
    }


def semantic_baseline(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """返回参与门禁比较的稳定语义视图，并兼容读取旧 fixture。"""
    if path.name == RUNTIME_BASELINE_PATH.name:
        return _migrate_runtime_baseline(value)
    if path.name == PLUGIN_BASELINE_PATH.name:
        migrated = _migrate_plugin_baseline(value)
        return {
            key: item
            for key, item in migrated.items()
            if key != "provenance"
        }
    return value


def transaction_ratchet_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    """事务债务只允许删除既有条目，不允许新增或提高任一分类计数。"""
    if expected.get("schema_version") != actual.get("schema_version"):
        return False
    if expected.get("scope") != actual.get("scope"):
        return False
    sections = (
        ("model_decorators", "methods"),
        ("model_transaction_calls", "calls"),
        ("model_session_factories", "calls"),
        ("oper_transaction_calls", "calls"),
        ("oper_session_factories", "calls"),
    )
    for section, entries_key in sections:
        expected_section = expected.get(section, {})
        actual_section = actual.get(section, {})
        if actual_section.get("count", 0) > expected_section.get("count", 0):
            return False
        expected_entries = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in expected_section.get(entries_key, [])
        }
        actual_entries = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in actual_section.get(entries_key, [])
        }
        if not actual_entries.issubset(expected_entries):
            return False
    expected_kinds = expected.get("model_decorators", {}).get("by_kind", {})
    actual_kinds = actual.get("model_decorators", {}).get("by_kind", {})
    return all(
        count <= expected_kinds.get(decorator, 0)
        for decorator, count in actual_kinds.items()
    )


def configuration_ratchet_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    """配置债务只允许删除既有文件或构造点，不允许新增直接依赖。"""
    if expected.get("schema_version") != actual.get("schema_version"):
        return False
    if expected.get("scope") != actual.get("scope"):
        return False
    sections = (
        ("settings_imports", "files"),
        ("system_config_oper_constructions", "calls"),
        ("foundational_settings_boundaries", "entries"),
        ("composition_root_oper_boundaries", "entries"),
    )
    for section, entries_key in sections:
        expected_section = expected.get(section, {})
        actual_section = actual.get(section, {})
        if actual_section.get("count", 0) > expected_section.get("count", 0):
            return False
        expected_entries = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in expected_section.get(entries_key, [])
        }
        actual_entries = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in actual_section.get(entries_key, [])
        }
        if not actual_entries.issubset(expected_entries):
            return False
    return True


def _compare_semantic_values(
    expected: Any,
    actual: Any,
    path: str,
    report: dict[str, list[dict[str, Any]]],
) -> None:
    """递归比较语义 JSON，把增删改记录为可审查条目。"""
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_keys = set(expected)
        actual_keys = set(actual)
        for key in sorted(expected_keys - actual_keys):
            report["removed"].append(
                {"path": f"{path}.{key}", "value": expected[key]}
            )
        for key in sorted(actual_keys - expected_keys):
            report["added"].append(
                {"path": f"{path}.{key}", "value": actual[key]}
            )
        for key in sorted(expected_keys & actual_keys):
            _compare_semantic_values(
                expected[key],
                actual[key],
                f"{path}.{key}",
                report,
            )
        return
    if isinstance(expected, list) and isinstance(actual, list):
        expected_keys = [
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in expected
        ]
        actual_keys = [
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in actual
        ]
        expected_counts = Counter(expected_keys)
        actual_counts = Counter(actual_keys)
        for key in sorted((expected_counts - actual_counts).elements()):
            report["removed"].append({"path": path, "value": json.loads(key)})
        for key in sorted((actual_counts - expected_counts).elements()):
            report["added"].append({"path": path, "value": json.loads(key)})
        return
    if expected != actual:
        report["changed"].append(
            {"path": path, "expected": expected, "actual": actual}
        )


def build_comparison_report(path: Path, actual: dict[str, Any]) -> dict[str, Any]:
    """生成包含语义增删改和 provenance 的机器可读审查报告。"""
    expected = json.loads(path.read_text(encoding="utf-8"))
    expected_semantic = semantic_baseline(path, expected)
    actual_semantic = semantic_baseline(path, actual)
    differences: dict[str, list[dict[str, Any]]] = {
        "added": [],
        "removed": [],
        "changed": [],
    }
    _compare_semantic_values(expected_semantic, actual_semantic, "$", differences)
    semantic_match = expected_semantic == actual_semantic
    if path.name == TRANSACTION_BASELINE_PATH.name:
        semantic_match = transaction_ratchet_matches(expected, actual)
    elif path.name == CONFIGURATION_BASELINE_PATH.name:
        semantic_match = configuration_ratchet_matches(expected, actual)
    return {
        "baseline": str(_display_path(path)),
        "semantic_match": semantic_match,
        "expected_provenance": expected.get("provenance"),
        "actual_provenance": actual.get("provenance"),
        **differences,
    }


def check_json(
    path: Path,
    actual: dict[str, Any],
    *,
    write_hint: str,
) -> bool:
    """比较当前扫描结果和已提交基线并输出限定范围的更新提示。"""
    expected = json.loads(path.read_text(encoding="utf-8"))
    if path.name == TRANSACTION_BASELINE_PATH.name:
        if transaction_ratchet_matches(expected, actual):
            if expected != actual:
                print(
                    "事务债务已下降；门禁继续通过，可在本任务提交中显式运行 "
                    "scripts/architecture/baseline.py --write-host 固化新低水位",
                    file=sys.stderr,
                )
            return True
        print(
            f"事务债务出现新增：{_display_path(path)}；"
            "Model 自动事务、直接 commit/rollback 或 Oper 自建 Session 不得增长",
            file=sys.stderr,
        )
        return False
    if path.name == CONFIGURATION_BASELINE_PATH.name:
        if configuration_ratchet_matches(expected, actual):
            if expected != actual:
                print(
                    "配置债务已下降；门禁继续通过，可在本任务提交中显式运行 "
                    "scripts/architecture/baseline.py --write-host 固化新低水位",
                    file=sys.stderr,
                )
            return True
        print(
            f"配置债务出现新增：{_display_path(path)}；"
            "宿主直接 settings 导入或 SystemConfigOper 构造不得增长",
            file=sys.stderr,
        )
        return False
    expected_semantic = semantic_baseline(path, expected)
    actual_semantic = semantic_baseline(path, actual)
    if expected_semantic == actual_semantic:
        if expected != actual:
            print(
                f"基线语义未变化，但 schema/provenance 已变化：{_display_path(path)}",
                file=sys.stderr,
            )
        return True
    print(
        f"架构基线已变化：{_display_path(path)}；"
        f"确认变更符合边界后运行 scripts/architecture/baseline.py {write_hint}",
        file=sys.stderr,
    )
    return False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析限定宿主或插件范围的基线操作参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-host", action="store_true", help="校验宿主架构基线")
    action.add_argument("--check-plugins", action="store_true", help="校验官方插件基线")
    action.add_argument("--write-host", action="store_true", help="写入宿主架构基线")
    action.add_argument("--write-plugins", action="store_true", help="写入官方插件基线")
    action.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    action.add_argument("--write", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scope",
        choices=("host", "plugins"),
        help="旧 --check/--write 的必填兼容范围",
    )
    parser.add_argument(
        "--plugin-repo",
        type=Path,
        help="官方插件操作所需的独立 MoviePilot-Plugins 仓路径",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="随宿主检查打印当前运行契约源码位置",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="将检查结果写为独立 JSON 报告，不修改任何 fixture",
    )
    args = parser.parse_args(argv)
    if args.check or args.write:
        if not args.scope:
            parser.error("旧 --check/--write 已弃用，必须同时指定 --scope host|plugins")
        replacement = f"--{'check' if args.check else 'write'}-{args.scope}"
        print(
            f"警告：--{'check' if args.check else 'write'} --scope {args.scope} "
            f"已弃用，请改用 {replacement}",
            file=sys.stderr,
        )
        setattr(args, f"{'check' if args.check else 'write'}_{args.scope}", True)
    elif args.scope:
        parser.error("--scope 只能与旧 --check/--write 一起使用")
    plugin_action = args.check_plugins or args.write_plugins
    if plugin_action and not args.plugin_repo:
        parser.error("插件基线操作必须指定 --plugin-repo")
    if not plugin_action and args.plugin_repo:
        parser.error("--plugin-repo 只能用于插件基线操作")
    if args.diagnostics and not args.check_host:
        parser.error("--diagnostics 只能与 --check-host 一起使用")
    if args.report and not (args.check_host or args.check_plugins):
        parser.error("--report 只能与检查操作一起使用")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    """只对显式选择的宿主或插件基线执行检查或写入。"""
    args = parse_args(argv)
    host_action = args.check_host or args.write_host
    if host_action:
        baselines = [
            (DEPENDENCY_BASELINE_PATH, collect_dependency_baseline()),
            (RUNTIME_BASELINE_PATH, collect_runtime_baseline()),
            (TRANSACTION_BASELINE_PATH, collect_transaction_debt_baseline()),
            (CONFIGURATION_BASELINE_PATH, collect_configuration_debt_baseline()),
        ]
        write_hint = "--write-host"
    else:
        plugin_repo = args.plugin_repo.resolve()
        if not plugin_repo.is_dir():
            raise SystemExit(f"插件仓不存在：{plugin_repo}")
        baselines = [
            (PLUGIN_BASELINE_PATH, collect_official_plugin_baseline(plugin_repo))
        ]
        write_hint = f"--write-plugins --plugin-repo {plugin_repo}"
    if args.write_host or args.write_plugins:
        display_paths = ", ".join(
            str(_display_path(path)) for path, _baseline in baselines
        )
        print(f"即将写入：{display_paths}")
        for path, baseline in baselines:
            write_json(path, baseline)
            print(f"已写入 {_display_path(path)}")
        return 0
    checks = [
        check_json(path, baseline, write_hint=write_hint)
        for path, baseline in baselines
    ]
    if args.report:
        report_path = args.report.resolve()
        report_value = {
            "schema_version": 1,
            "checks": [
                build_comparison_report(path, baseline)
                for path, baseline in baselines
            ],
        }
        print(f"即将写入报告：{_display_path(report_path)}")
        write_json(report_path, report_value)
        print(f"已写入报告：{_display_path(report_path)}")
    if args.diagnostics:
        print(json.dumps(collect_runtime_diagnostics(), ensure_ascii=False, indent=2))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
