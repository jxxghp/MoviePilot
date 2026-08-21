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
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"
BASELINE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "architecture"
DEPENDENCY_BASELINE_PATH = BASELINE_ROOT / "dependency-baseline.json"
RUNTIME_BASELINE_PATH = BASELINE_ROOT / "runtime-contract-baseline.json"
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


def iter_import_candidates(
    module_name: str,
    path: Path,
) -> list[tuple[str, Optional[str]]]:
    """提取模块导入候选，第二项记录 from-import 的具体符号。"""
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    candidates: list[tuple[str, Optional[str]]] = []
    for node in ast.walk(parse_source(path)):
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
    return {
        "schema_version": 1,
        "scope": "MoviePilot host app excluding app/plugins",
        "module_count": len(modules),
        "edge_count": len(edges),
        "edge_sha256": digest,
        "modules": sorted(modules),
        "edges": edges,
        "strongly_connected_components": strongly_connected_components(graph),
        "boundary_edges": collect_boundary_edges(graph, modules),
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


def _event_reference(node: ast.AST) -> str | None:
    """从 AST 节点解析 EventType/ChainEventType 的静态成员引用。"""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"EventType", "ChainEventType"}
    ):
        return f"{node.value.id}.{node.attr}"
    return None


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


def _collect_event_locations() -> tuple[
    list[str],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """扫描事件枚举及其生产、消费位置，供语义和诊断视图复用。"""
    event_members = _event_enum_members("EventType")
    chain_event_members = _event_enum_members("ChainEventType")

    producers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    consumers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dynamic_producers: list[dict[str, Any]] = []
    dynamic_consumers: list[dict[str, Any]] = []
    for module_name, path in discover_modules().items():
        tree = parse_source(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func,
                ast.Attribute,
            ):
                continue
            location = {"caller": module_name, "line": node.lineno}
            if node.func.attr in {"send_event", "async_send_event"}:
                reference = _event_reference(node.args[0]) if node.args else None
                if reference:
                    producers[reference].append(location)
                else:
                    dynamic_producers.append(location)
                continue
            if node.func.attr not in {"register", "add_event_listener"}:
                continue
            references: list[str] = []
            if node.args:
                target = node.args[0]
                if reference := _event_reference(target):
                    references.append(reference)
                elif isinstance(target, (ast.List, ast.Tuple)):
                    references.extend(
                        reference
                        for item in target.elts
                        if (reference := _event_reference(item))
                    )
                elif (
                    isinstance(target, ast.Name)
                    and target.id in {"EventType", "ChainEventType"}
                ):
                    enum_members = (
                        event_members
                        if target.id == "EventType"
                        else chain_event_members
                    )
                    references.extend(
                        f"{target.id}.{member}" for member in enum_members
                    )
            if references:
                for reference in references:
                    consumers[reference].append(location)
            else:
                dynamic_consumers.append(location)

    enum_names = [
        *(f"EventType.{member}" for member in event_members),
        *(f"ChainEventType.{member}" for member in chain_event_members),
    ]
    return (
        enum_names,
        producers,
        consumers,
        dynamic_producers,
        dynamic_consumers,
    )


def collect_event_contracts() -> dict[str, Any]:
    """收集不受源码行号变化影响的宿主事件语义契约。"""
    (
        enum_names,
        producers,
        consumers,
        dynamic_producers,
        dynamic_consumers,
    ) = _collect_event_locations()
    contracts = {
        name: {
            "producers": _aggregate_locations(
                producers.get(name, []),
                ("caller",),
            ),
            "consumers": _aggregate_locations(
                consumers.get(name, []),
                ("caller",),
            ),
        }
        for name in sorted(enum_names)
    }
    return {
        "event_count": len(contracts),
        "producer_count": sum(len(items) for items in producers.values()),
        "consumer_count": sum(len(items) for items in consumers.values()),
        "events": contracts,
        "dynamic_producers": _aggregate_locations(dynamic_producers, ("caller",)),
        "dynamic_consumers": _aggregate_locations(dynamic_consumers, ("caller",)),
    }


def collect_event_diagnostics() -> dict[str, Any]:
    """收集事件生产与消费的当前源码位置，仅用于人工诊断。"""
    (
        enum_names,
        producers,
        consumers,
        dynamic_producers,
        dynamic_consumers,
    ) = _collect_event_locations()
    return {
        "events": {
            name: {
                "producers": sorted(
                    producers.get(name, []),
                    key=lambda item: (item["caller"], item["line"]),
                ),
                "consumers": sorted(
                    consumers.get(name, []),
                    key=lambda item: (item["caller"], item["line"]),
                ),
            }
            for name in sorted(enum_names)
        },
        "dynamic_producers": sorted(
            dynamic_producers,
            key=lambda item: (item["caller"], item["line"]),
        ),
        "dynamic_consumers": sorted(
            dynamic_consumers,
            key=lambda item: (item["caller"], item["line"]),
        ),
    }


def collect_sdk_exports() -> dict[str, list[dict[str, str]]]:
    """通过 AST 收集顶层 SDK 公开符号，避免导入时物化运行资源。"""
    result: dict[str, list[dict[str, str]]] = {}
    for path in sorted((APP_ROOT / "sdk").glob("*.py")):
        module_name = f"app.sdk.{path.stem}" if path.stem != "__init__" else "app.sdk"
        exports: list[dict[str, str]] = []
        for node in parse_source(path).body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    public_name = alias.asname or alias.name
                    if public_name.startswith("_") or alias.name == "*":
                        continue
                    exports.append(
                        {
                            "name": public_name,
                            "kind": "import",
                            "target": f"{node.module}.{alias.name}",
                        }
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    exports.append(
                        {"name": node.name, "kind": type(node).__name__, "target": ""}
                    )
        result[module_name] = sorted(
            exports,
            key=lambda item: (item["name"], item["kind"], item["target"]),
        )
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
        "schema_version": 2,
        "run_module": collect_run_module_contracts(),
        "events": collect_event_contracts(),
        "sdk_exports": collect_sdk_exports(),
        "compat_manifest": collect_compat_manifest(),
    }


def collect_runtime_diagnostics() -> dict[str, Any]:
    """生成带当前源码行号的运行契约诊断视图，不写入语义 fixture。"""
    return {
        "run_module": collect_run_module_diagnostics(),
        "events": collect_event_diagnostics(),
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


def collect_official_plugin_baseline(plugin_repo: Path) -> dict[str, Any]:
    """扫描独立官方插件仓的导入面、Hook 和动态 API 契约。"""
    roots = [plugin_repo / "plugins.v2", plugin_repo / "plugins.v3"]
    paths = sorted(
        path
        for root in roots
        if root.exists()
        for path in root.rglob("*.py")
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
            "roots": [root.name for root in roots],
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


def _migrate_runtime_baseline(value: dict[str, Any]) -> dict[str, Any]:
    """把包含源码行号的 v1 运行契约转换为稳定语义结构。"""
    if value.get("schema_version") != 1:
        return value
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


def check_json(
    path: Path,
    actual: dict[str, Any],
    *,
    write_hint: str,
) -> bool:
    """比较当前扫描结果和已提交基线并输出限定范围的更新提示。"""
    expected = json.loads(path.read_text(encoding="utf-8"))
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
    return args


def main(argv: Optional[list[str]] = None) -> int:
    """只对显式选择的宿主或插件基线执行检查或写入。"""
    args = parse_args(argv)
    host_action = args.check_host or args.write_host
    if host_action:
        baselines = [
            (DEPENDENCY_BASELINE_PATH, collect_dependency_baseline()),
            (RUNTIME_BASELINE_PATH, collect_runtime_baseline()),
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
    if args.diagnostics:
        print(json.dumps(collect_runtime_diagnostics(), ensure_ascii=False, indent=2))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
