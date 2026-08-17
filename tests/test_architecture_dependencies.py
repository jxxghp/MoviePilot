import ast
from functools import lru_cache
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
LEGACY_ROOTS = ("app.core", "app.helper", "app.utils")
LEGACY_MODULES = {"app.log"}
IMPLEMENTATION_ROOTS = (
    "app.agent.skills",
    "app.adapters",
    "app.application",
    "app.domain",
    "app.foundation",
    "app.runtime",
)
CYCLE_ROOTS = (*IMPLEMENTATION_ROOTS, "app.runtime.compat", "app.sdk")
RETIRED_CANONICAL_ROOTS = (
    "compat",
    "extensions",
    "infrastructure",
    "integrations",
    "messaging",
    "platform",
    "security",
    "services",
)
RETIRED_CANONICAL_FILES = (
    "app/infrastructure/package_installer.py",
    "app/infrastructure/resource_updater.py",
    "app/infrastructure/rust_accel.py",
    "app/messaging/agent_bridge.py",
    "app/platform/config_reload.py",
    "app/platform/rate_limit.py",
    "app/platform/thread_pool.py",
    "app/services/filter_rules.py",
    "app/services/transfer_history.py",
    "app/extensions/module_loader.py",
    "app/extensions/plugin_market.py",
    "app/extensions/plugin_repository.py",
    "app/infrastructure/http.py",
    "app/integrations/rss.py",
    "app/security/two_factor.py",
    "app/infrastructure/gc.py",
    "app/infrastructure/web.py",
    "app/security/url_safety.py",
    "app/domain/mediaserver.py",
    "app/domain/nfo.py",
    "app/domain/string.py",
    "app/log.py",
    "app/foundation/diagnostics.py",
    "app/infrastructure/log.py",
    "app/startup/diagnostics_initializer.py",
    "app/startup/log_initializer.py",
    "app/messaging/notification.py",
    "app/messaging/webpush.py",
    "app/foundation/jieba.py",
    "app/foundation/module.py",
    "app/foundation/object.py",
    "app/foundation/structures.py",
    "app/foundation/zhconv.py",
    "app/runtime/runtime.py",
    "app/adapters/network/rss.py",
    "app/adapters/network/sites.pyi",
)
PLUGIN_COMPONENT_ROOTS = (
    "app/adapters/external/plugin",
    "app/adapters/system/plugin",
    "app/application/plugin",
    "app/runtime/extensions/plugin",
)
PLUGIN_LEGACY_ABI_NAMES = {
    "MoviePilotServerHelper",
    "PluginHelper",
    "PluginManager",
}
FORBIDDEN_IMPORT_PREFIXES = {
    "app.foundation": (
        "app.adapters",
        "app.application",
        "app.db",
        "app.domain",
        "app.runtime",
        "app.sdk",
    ),
    "app.domain": (
        "app.adapters",
        "app.application",
        "app.db",
        "app.runtime",
        "app.sdk",
    ),
    "app.adapters": (
        "app.application",
        "app.runtime.compat",
        "app.runtime.extensions",
        "app.sdk",
    ),
    "app.runtime": (
        "app.application",
        "app.sdk",
    ),
    "app.application": (
        "app.runtime.compat",
        "app.sdk",
    ),
}


def _discover_modules() -> dict[str, Path]:
    """建立实际 Python 模块名到源码路径的映射。

    `app/plugins/` 由插件仓自治（包含独立第三方实现与未完成文件），
    不参与宿主架构图分析。
    """
    modules: dict[str, Path] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[0] == "app" and parts[1] == "plugins":
            continue
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _resolve_imports(
    module_name: str,
    path: Path,
    known_modules: set[str],
) -> set[str]:
    """解析一个模块的静态导入，并计入 Python 必然初始化的父包。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = package.split(".")
                base = ".".join(package_parts[: len(package_parts) - node.level + 1])
                imported_module = ".".join(
                    part for part in (base, node.module or "") if part
                )
            else:
                imported_module = node.module or ""
            if imported_module:
                candidates.append(imported_module)
                candidates.extend(
                    f"{imported_module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )

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


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[set[str]]:
    """使用 Tarjan 算法返回依赖图中的非平凡强连通分量。"""
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(module_name: str) -> None:
        """深度遍历一个模块并在根节点处收集强连通分量。"""
        indices[module_name] = len(indices)
        low_links[module_name] = indices[module_name]
        stack.append(module_name)
        on_stack.add(module_name)
        for dependency in graph[module_name]:
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
        component: set[str] = set()
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.add(dependency)
            if dependency == module_name:
                break
        if len(component) > 1:
            components.append(component)

    for module_name in sorted(graph):
        if module_name not in indices:
            visit(module_name)
    return components


def _legacy_imports(path: Path) -> set[str]:
    """提取源码中的静态和常量动态旧路径导入。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            candidates.append(node.module)
        elif isinstance(node, ast.Call) and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                candidates.append(argument.value)
        imports.update(
            candidate
            for candidate in candidates
            if candidate in LEGACY_MODULES or candidate.startswith(LEGACY_ROOTS)
        )
    return imports


def test_legacy_roots_contain_no_python_sources():
    """旧目录只能作为运行时虚拟包存在，仓库中不得重新出现源码。"""
    leftovers = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for root_name in ("core", "helper", "utils")
        for path in (APP_ROOT / root_name).rglob("*.py")
    )
    assert leftovers == []


def test_legacy_source_directories_do_not_exist():
    """core/helper/utils 物理目录应完全退役，旧导入只由虚拟兼容包解析。"""
    leftovers = [
        root_name
        for root_name in ("core", "helper", "utils")
        if (APP_ROOT / root_name).exists()
    ]
    assert leftovers == []


def test_retired_canonical_filenames_do_not_return():
    """能力包应使用包内语境明确的短文件名，避免再次出现冗余角色后缀。"""
    leftovers = [
        relative_path
        for relative_path in RETIRED_CANONICAL_FILES
        if (PROJECT_ROOT / relative_path).exists()
    ]
    assert leftovers == []


def test_retired_canonical_roots_contain_no_python_sources():
    """已收敛的新增目录不得再次以顶级 Python 包形式出现。"""
    leftovers = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for root_name in RETIRED_CANONICAL_ROOTS
        for path in (APP_ROOT / root_name).rglob("*.py")
    )
    assert leftovers == []


def test_host_code_does_not_import_legacy_roots():
    """除插件和兼容层外，宿主代码必须使用 canonical 路径。"""
    violations: dict[str, set[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "plugins" or relative.parts[:2] == (
            "runtime",
            "compat",
        ):
            continue
        imports = _legacy_imports(path)
        if imports:
            violations[str(relative)] = imports
    assert violations == {}


def test_plugin_components_do_not_reexport_legacy_abi_names():
    """新插件组件只提供 canonical 能力，不得复制旧 Helper、Manager 或 Oper 导出。"""
    violations: list[str] = []
    for root in PLUGIN_COMPONENT_ROOTS:
        for path in (PROJECT_ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == "__getattr__" or node.name in PLUGIN_LEGACY_ABI_NAMES:
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.name}")
                elif isinstance(node, ast.ClassDef):
                    if node.name in PLUGIN_LEGACY_ABI_NAMES or node.name.endswith("Oper"):
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.name}")
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        is_legacy_name = (
                            alias.name in PLUGIN_LEGACY_ABI_NAMES
                            or alias.name.endswith("Oper")
                        )
                        is_private = bool(alias.asname and alias.asname.startswith("_"))
                        if is_legacy_name and not is_private:
                            violations.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{alias.name}"
                            )
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = {
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    }
                    forbidden = {
                        name
                        for name in names
                        if name == "__all__"
                        or name in PLUGIN_LEGACY_ABI_NAMES
                        or name.endswith("Oper")
                    }
                    violations.extend(
                        f"{path.relative_to(PROJECT_ROOT)}:{name}"
                        for name in sorted(forbidden)
                    )
    assert violations == []


def test_host_code_uses_precise_schema_modules():
    """宿主不得重新依赖 schema 聚合入口或星号导出。"""
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] in {"plugins", "schemas"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app":
                if any(alias.name == "schemas" for alias in node.names):
                    violations.append(str(relative))
                    break
            if isinstance(node, ast.ImportFrom) and node.module == "app.schemas":
                violations.append(str(relative))
                break
    assert violations == []


def test_database_internals_do_not_import_db_facades():
    """DB 子模块必须依赖具体实现文件，不得回流到包级兼容入口。"""
    violations: list[str] = []
    for path in (APP_ROOT / "db").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module in {"app.db", "app.db.models"}
            for node in ast.walk(tree)
        ):
            violations.append(str(path.relative_to(PROJECT_ROOT)))
    assert violations == []


def test_migrated_modules_are_not_in_import_cycles():
    """任何 canonical 迁移模块都不得进入完整应用依赖图的环。"""
    modules = _discover_modules()
    known_modules = set(modules)
    graph = {
        name: _resolve_imports(name, path, known_modules)
        for name, path in modules.items()
    }
    relevant_cycles = [
        sorted(component)
        for component in _strongly_connected_components(graph)
        if any(name.startswith(CYCLE_ROOTS) for name in component)
    ]
    assert relevant_cycles == []


def test_canonical_layers_do_not_depend_on_sdk_or_compat():
    """canonical 实现层不得反向依赖面向插件的 SDK 或兼容层。"""
    violations: dict[str, set[str]] = {}
    modules = _discover_modules()
    known_modules = set(modules)
    for module_name, path in modules.items():
        if not module_name.startswith(IMPLEMENTATION_ROOTS):
            continue
        if module_name.startswith("app.runtime.compat"):
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith(("app.sdk", "app.runtime.compat"))
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_capability_packages_do_not_import_forbidden_upper_layers():
    """新能力包只能依赖明确允许的下层或同层协作包。"""
    modules = _discover_modules()
    known_modules = set(modules)
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        source_root = next(
            (
                root
                for root in FORBIDDEN_IMPORT_PREFIXES
                if module_name == root or module_name.startswith(f"{root}.")
            ),
            None,
        )
        if not source_root:
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith(FORBIDDEN_IMPORT_PREFIXES[source_root])
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_site_domain_uses_foundation_dom_boundary():
    """站点领域规则应依赖 DOM 原语，不得重新耦合聚合字符串工具。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.domain.site",
        modules["app.domain.site"],
        set(modules),
    )
    assert "app.foundation.dom" in dependencies
    assert "app.domain.string" not in dependencies


def test_host_code_does_not_use_string_utils_facade():
    """聚合 StringUtils 只服务插件兼容，宿主实现必须使用拆分后的能力。"""
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] in {"plugins", "sdk"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if any(isinstance(node, ast.Name) and node.id == "StringUtils" for node in ast.walk(tree)):
            violations.append(str(relative))
    assert violations == []


def test_runtime_log_is_a_dependency_leaf():
    """底层可引用运行时日志，但日志模块本身不得反向导入应用模块。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.runtime.log",
        modules["app.runtime.log"],
        set(modules),
    )
    assert {
        dependency
        for dependency in dependencies
        if dependency.startswith("app.")
    } == set()


def test_foundation_does_not_emit_runtime_logs():
    """基础机制不打印或初始化日志系统，运行期诊断由上层调用方负责。"""
    violations: list[str] = []
    for path in (APP_ROOT / "foundation").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "logging" for alias in node.names
            ):
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
            if isinstance(node, ast.ImportFrom) and node.module in {
                "logging",
                "app.runtime.log",
            }:
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
    assert violations == []


def test_cache_contract_does_not_import_concrete_adapters():
    """运行时缓存契约和内存机制不得反向导入具体缓存适配器。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.runtime.cache",
        modules["app.runtime.cache"],
        set(modules),
    )
    assert {
        dependency
        for dependency in dependencies
        if dependency.startswith("app.adapters.cache")
    } == set()


def test_resource_adapter_does_not_restart_process():
    """资源下载安装适配器不得反向调用进程重启能力。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.adapters.system.resource",
        modules["app.adapters.system.resource"],
        set(modules),
    )
    assert "app.runtime.state" not in dependencies


def test_modules_do_not_import_other_modules_or_chain():
    """模块之间以及模块对链层的直接依赖被禁止，跨模块编排归链层。

    `app.modules._base` 是模块共享样板基类包（模块发现会跳过），不视为业务模块。
    """
    modules = _discover_modules()
    known_modules = set(modules)
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        if not module_name.startswith("app.modules."):
            continue
        own_package = module_name.split(".")[2]
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.chain")
            or (
                dependency.startswith("app.modules.")
                and dependency.split(".")[2] not in (own_package, "_base")
            )
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_entrypoints_do_not_import_module_internals():
    """入口层不得穿透导入具体模块实现，应经由链层或应用服务。"""
    modules = _discover_modules()
    known_modules = set(modules)
    entrypoint_roots = ("app.api", "app.agent", "app.monitor", "app.workflow", "app.doctor")
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        if not module_name.startswith(entrypoint_roots):
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.modules.")
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_chain_does_not_import_downloader_sdks():
    """链层不得引入下载器后端协议类型，避免后端细节泄漏到编排层。"""
    forbidden_sdks = {"qbittorrentapi", "transmission_rpc"}
    violations: list[str] = []
    for path in (APP_ROOT / "chain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.append(node.module)
            if any(name.split(".")[0] in forbidden_sdks for name in names):
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
    assert violations == []


def test_chain_does_not_import_module_internals():
    """链层与模块只能通过 run_module 方法名契约互联，禁止直接导入模块实现。

    模块内容必须封闭在模块内部，链层不显式指定具体模块的类、异常或常量，
    这样模块才是可插拔的。
    """
    modules = _discover_modules()
    known_modules = set(modules)
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        if not module_name.startswith("app.chain"):
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.modules.")
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


@lru_cache(maxsize=1)
def _build_module_graph() -> dict[str, set[str]]:
    """构建非插件模块的完整静态依赖图，供既有包治理断言复用。

    纯静态 AST 分析无副作用，结果可安全缓存；多断言共享一次解析。
    """
    modules = _discover_modules()
    known_modules = set(modules)
    return {
        name: _resolve_imports(name, path, known_modules)
        for name, path in modules.items()
    }


def test_chain_does_not_import_agent_implementation():
    """编排层不得反向依赖 Agent 实现，跨域编排经 application 门面。"""
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        if not module_name.startswith("app.chain"):
            continue
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.agent")
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_agent_application_facade_does_not_import_agent_implementation():
    """Agent application 门面只能接收组合根注入，不能反向解析具体实现。"""
    dependencies = _build_module_graph()["app.application.agent"]
    assert {
        dependency
        for dependency in dependencies
        if dependency.startswith("app.agent")
    } == set()


def test_agent_tools_do_not_import_entrypoint_internals():
    """Agent 工具不得穿透导入 HTTP 端点、调度器与命令注册表内部实现。

    工具对进程级状态的读写必须收敛到 application 门面，
    否则 agent 层与入口层互相穿透会形成不可测试的循环。
    """
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        if not module_name.startswith("app.agent.tools"):
            continue
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith(("app.api", "app.scheduler", "app.command"))
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_api_does_not_import_factory():
    """装配器（factory）只允许 app.main 使用，HTTP 端点不得回引。"""
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        if not module_name.startswith("app.api"):
            continue
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.factory")
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


PROCESS_LEVEL_ROOTS = (
    "app.api",
    "app.chain",
    "app.agent",
    "app.scheduler",
    "app.command",
    "app.monitor",
    "app.startup",
    "app.factory",
)


def test_process_level_packages_are_not_mutually_cyclic():
    """进程级根包之间不得形成跨包强连通分量。

    允许的环只存在于：单一包内部（modules 模块内、db 内、schemas 包内、
    agent 子域内、doctor 内）。跨根包的环意味着入口层、编排层与 Agent 层
    互相穿透，破坏可插拔性与可测试性。
    """
    graph = _build_module_graph()
    components = _strongly_connected_components(graph)
    violations: list[list[str]] = []
    for component in components:
        roots = {
            name.split(".")[1]
            for name in component
            if name.startswith("app.") and name.count(".") >= 1
        }
        involved = {root for root in roots if f"app.{root}" in PROCESS_LEVEL_ROOTS}
        if len(involved) > 1:
            violations.append(sorted(component))
    assert violations == []
