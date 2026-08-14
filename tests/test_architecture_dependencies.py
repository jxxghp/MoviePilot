import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
LEGACY_ROOTS = ("app.core", "app.helper", "app.utils")
LEGACY_MODULES = {"app.log"}
IMPLEMENTATION_ROOTS = (
    "app.agent.skills",
    "app.domain",
    "app.extensions",
    "app.foundation",
    "app.infrastructure",
    "app.integrations",
    "app.messaging",
    "app.platform",
    "app.security",
    "app.services",
)
CYCLE_ROOTS = (*IMPLEMENTATION_ROOTS, "app.compat", "app.sdk")
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
    "app/log.py",
    "app/foundation/diagnostics.py",
    "app/infrastructure/log.py",
    "app/startup/diagnostics_initializer.py",
    "app/startup/log_initializer.py",
    "app/messaging/notification.py",
    "app/messaging/webpush.py",
)
FORBIDDEN_CAPABILITY_IMPORTS = {
    "foundation": {
        "domain", "extensions", "infrastructure", "integrations", "messaging",
        "platform", "security", "services", "sdk", "compat", "log",
    },
    "domain": {
        "extensions", "integrations", "messaging", "security", "services",
        "sdk", "compat", "db", "infrastructure", "platform", "log",
    },
    "platform": {
        "domain", "extensions", "integrations", "messaging", "security",
        "services", "sdk", "compat",
    },
    "infrastructure": {
        "domain", "extensions", "integrations", "messaging", "security",
        "services", "sdk", "compat",
    },
    "extensions": {"messaging", "security", "services", "sdk", "compat"},
    "integrations": {
        "extensions", "messaging", "security", "services", "sdk", "compat",
    },
    "messaging": {"integrations", "security", "services", "sdk", "compat"},
    "security": {"extensions", "messaging", "services", "sdk", "compat"},
    "services": {"integrations", "messaging", "sdk", "compat"},
    "compat": {
        "domain", "extensions", "foundation", "infrastructure", "integrations",
        "messaging", "platform", "security", "services", "sdk",
    },
}


def _discover_modules() -> dict[str, Path]:
    """建立实际 Python 模块名到源码路径的映射。"""
    modules: dict[str, Path] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).with_suffix("")
        parts = list(relative.parts)
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


def test_retired_canonical_filenames_do_not_return():
    """能力包应使用包内语境明确的短文件名，避免再次出现冗余角色后缀。"""
    leftovers = [
        relative_path
        for relative_path in RETIRED_CANONICAL_FILES
        if (PROJECT_ROOT / relative_path).exists()
    ]
    assert leftovers == []


def test_host_code_does_not_import_legacy_roots():
    """除插件和兼容层外，宿主代码必须使用 canonical 路径。"""
    violations: dict[str, set[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] in {"compat", "plugins"}:
            continue
        imports = _legacy_imports(path)
        if imports:
            violations[str(relative)] = imports
    assert violations == {}


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
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith(("app.sdk", "app.compat"))
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_capability_packages_do_not_import_forbidden_upper_layers():
    """能力包只能依赖其明确允许的下层或同层协作包。"""
    modules = _discover_modules()
    known_modules = set(modules)
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        parts = module_name.split(".")
        if len(parts) < 2:
            continue
        source_package = parts[1]
        forbidden_packages = FORBIDDEN_CAPABILITY_IMPORTS.get(source_package)
        if not forbidden_packages:
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if len(dependency.split(".")) >= 2
            and dependency.split(".")[1] in forbidden_packages
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_platform_log_is_a_dependency_leaf():
    """底层可引用平台日志，但日志模块本身不得反向导入应用模块。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.platform.log",
        modules["app.platform.log"],
        set(modules),
    )
    assert {
        dependency
        for dependency in dependencies
        if dependency.startswith("app.")
    } == set()


def test_foundation_does_not_emit_runtime_logs():
    """基础机制不初始化日志系统，运行期诊断由上层调用方负责。"""
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
                "app.platform.log",
            }:
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
    assert violations == []


def test_cache_contract_does_not_import_concrete_adapters():
    """平台缓存契约和内存机制不得反向导入基础设施适配器。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.platform.cache",
        modules["app.platform.cache"],
        set(modules),
    )
    assert {
        dependency
        for dependency in dependencies
        if dependency.startswith("app.infrastructure")
    } == set()


def test_resource_adapter_does_not_restart_process():
    """资源下载安装适配器不得反向调用进程重启能力。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.infrastructure.resource",
        modules["app.infrastructure.resource"],
        set(modules),
    )
    assert "app.platform.runtime" not in dependencies
