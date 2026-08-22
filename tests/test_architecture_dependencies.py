import ast
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
# 包级依赖矩阵：键是包，值是它允许 import 的包全集。
# 未列出的包（api / startup / cli 等边缘层与组合根）可依赖任意下层。
PACKAGE_LAYERS: dict[str, frozenset[str]] = {
    "foundation": frozenset(),
    "schemas": frozenset(),
    "domain": frozenset({"foundation", "schemas"}),
    "runtime": frozenset({"foundation", "schemas"}),
    "db": frozenset({"foundation", "schemas", "runtime"}),
    "adapters": frozenset({"foundation", "schemas", "domain", "runtime"}),
    "application": frozenset(
        {"foundation", "schemas", "domain", "runtime", "db", "adapters"}
    ),
    "modules": frozenset(
        {"foundation", "schemas", "domain", "runtime", "db", "adapters"}
    ),
    "workflow": frozenset(
        {
            "foundation", "schemas", "domain", "runtime", "db", "adapters",
            "application",
        }
    ),
    "monitor": frozenset(
        {
            "foundation", "schemas", "domain", "runtime", "db", "adapters",
            "application",
        }
    ),
    "doctor": frozenset({"foundation", "schemas", "domain", "runtime", "adapters"}),
    "agent": frozenset(
        {
            "foundation", "schemas", "domain", "runtime", "db", "adapters",
            "application",
        }
    ),
    "sdk": frozenset(
        {
            "foundation", "schemas", "domain", "runtime", "db", "adapters",
            "application",
        }
    ),
    "testing": frozenset({"application", "startup"}),
}
# 已知且被接受的方向负债：矩阵禁止但暂时保留的边，每条附清偿方向。
# 边消失后条目可直接删除，留着不会导致失败。
DEPENDENCY_DEBT: dict[tuple[str, str], str] = {
    ("sdk", "agent"): (
        "智能体工具基类 MoviePilotTool 现居 app.agent.tools.base，而扩展声明智能体工具时"
        "必须继承它，SDK 不给出口就只能让扩展直接 import 宿主内部路径。同一处安家还逼得"
        "app.runtime.extensions.admission.agent_tool 判不了继承，只能由启动"
        "组合根经 configure_agent_tool_base 在运行期注入基类。"
        "清偿方向：把该契约迁出 app.agent，SDK 与校验层即可直接 import。"
    ),
    ("sdk", "modules"): (
        "存储后端契约 StorageBase 现居 app.modules._base.storage，而扩展声明存储类型时"
        "必须继承它，SDK 不给出口就只能让扩展直接 import 宿主内部路径。同一处安家还逼得"
        "app.runtime.extensions.admission.storage 改用 MRO 限定名字符串比对。"
        "清偿方向：把该契约迁出 app.modules，两边即可直接 import。"
    ),
}
# 同一产品线变体模块允许依赖的兄弟模块包：变体只改存储/服务标识，
# 共用一份客户端实现，属于继承而非跨模块编排。
MODULE_VARIANT_DEPENDENCIES: dict[str, frozenset[str]] = {
    # AListGo 是 AList 的 Go 重写版，只有存储标识不同
    "alistgo": frozenset({"alist"}),
}
LEGACY_ROOTS = ("app.chain", "app.core", "app.helper", "app.utils")
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
    "app/application/plugins.py",
    "app/application/subscribe.py",
    "app/scheduler.py",
    "app/command.py",
)
# 空壳目录扫描豁免的 app/ 下顶级目录：目录内容由插件仓自治，属运行期数据。
SHELL_SCAN_EXEMPT_ROOTS = frozenset({"plugins"})
PLUGIN_COMPONENT_ROOTS = (
    "app/adapters/external/plugin",
    "app/adapters/system/plugin",
    "app/application/plugin",
    "app/runtime/extensions/admission",
    "app/runtime/extensions/contract",
    "app/runtime/extensions/lifecycle",
    "app/runtime/extensions/projection",
    "app/runtime/extensions/registry",
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
    """解析一个模块的静态导入，并计入 Python 必然初始化的父包。

    模块自身的祖先包不计入依赖：Python 必然先初始化它们，这条边由包结构决定
    而非模块的设计选择，计入只会把「父包 __init__ 导入子模块」记成环。
    祖先包被源码显式导入时仍照常计入。
    """
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
                and not module_name.startswith(f"{parent}.")
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
        for root_name in ("chain", "core", "helper", "utils")
        for path in (APP_ROOT / root_name).rglob("*.py")
    )
    assert leftovers == []


def test_legacy_source_directories_do_not_exist():
    """chain/core/helper/utils 物理目录应完全退役，旧导入只由虚拟兼容包解析。"""
    leftovers = [
        root_name
        for root_name in ("chain", "core", "helper", "utils")
        if (APP_ROOT / root_name).exists()
    ]
    assert leftovers == []


def _shell_directories() -> list[str]:
    """收集 `app/` 下不含任何真实文件的空壳目录。

    `__pycache__` 目录自身及其内容不计为真实文件；`SHELL_SCAN_EXEMPT_ROOTS`
    列出的顶级目录整棵子树不参与扫描。判据是目录递归为空而非是否含 `.py`：
    零 `.py` 的资源目录（`locales`、能力清单、人格预设）是合法的。

    返回：空壳目录相对仓库根的路径列表，按字典序排序。
    """
    directories: set[Path] = set()
    populated: set[Path] = set()
    for entry in APP_ROOT.rglob("*"):
        relative = entry.relative_to(APP_ROOT)
        if "__pycache__" in relative.parts:
            continue
        if relative.parts[0] in SHELL_SCAN_EXEMPT_ROOTS:
            continue
        if entry.is_dir():
            directories.add(entry)
        else:
            populated.update(entry.parents)
    return sorted(
        str(directory.relative_to(PROJECT_ROOT))
        for directory in directories - populated
    )


def test_no_shell_directories_remain():
    """切分或搬迁后不得留下空壳目录。

    没有 `__init__.py` 的目录仍是可导入的命名空间包，本仓的 `app/plugins/`
    正依赖这一行为。因此只剩 `__pycache__` 的目录不是碍眼而已：
    `import` 与 `importlib.util.find_spec` 对已退役路径继续成功并返回空包，
    凭 `ImportError` 判断路径是否退役的调用方会静默走错分支。
    """
    shells = _shell_directories()
    details = "\n".join(f"  - {shell}" for shell in shells)
    assert shells == [], (
        f"发现 {len(shells)} 个空壳目录（递归不含任何非 __pycache__ 文件）：\n"
        f"{details}\n"
        "判为残留的理由：目录在仓库中零文件，却仍是可被 import 解析的命名空间包，"
        "使已退役路径的 import 与 find_spec 继续成功。\n"
        "处理方式：连同目录内的 __pycache__ 一并删除该目录；"
        "若属插件仓运行期数据，应加入 SHELL_SCAN_EXEMPT_ROOTS。"
    )


def test_startup_root_admits_only_initializers():
    """组合根顶层只放初始化动作。

    判据 S（docs/rules/05-architecture.md）把 `app/startup/` 的成员分成四类：
    `lifecycle/` 决定时刻，顶层 `*_initializer.py` 在指定时刻执行一次，
    `bindings/` 由消费方按自己的时刻反复读取，`ports/` 由组合根构造后交给别处长期
    持有。顶层若没有形状约束，新文件的默认落点就是顶层——2024-09 到 2026-08-16
    之间新增的每一个文件都落在这里，四类因此混在一层，「谁在启动时被调用」只能靠
    逐个打开文件重建。
    """
    strays = sorted(
        path.name
        for path in (APP_ROOT / "startup").glob("*.py")
        if path.name != "__init__.py" and not path.name.endswith("_initializer.py")
    )
    assert strays == [], (
        f"app/startup/ 顶层出现非初始化动作模块：{strays}\n"
        "按判据 S 重新定位：决定其他成员运行时刻的进 lifecycle/；"
        "由消费方按自己的时刻反复读取的绑定表进 bindings/；"
        "三问皆不命中时先扩充判据 S，不得默认留在顶层。"
    )


def test_startup_subpackages_admit_no_initializers():
    """组合根子目录不得混入初始化动作。

    用 rglob 扫描而非逐个列出子目录：新增子包会自动进入覆盖范围，
    不会出现「加了目录、门禁还绿着但已不覆盖它」的静默失效。
    """
    startup_root = APP_ROOT / "startup"
    strays = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in startup_root.rglob("*_initializer.py")
        if path.parent != startup_root
    )
    assert strays == [], (
        f"组合根子目录出现初始化动作模块：{strays}\n"
        "按判据 S，在组合根指定时刻执行一次的动作平铺在 app/startup/ 顶层。"
    )


def test_startup_subpackages_are_declared():
    """判据 S 承认的组合根子目录是封闭集合。

    第三个子目录意味着出现了判据 S 四问之外的第四类成员。门禁在此转红，
    迫使新增者先在 docs/rules/05-architecture.md 里把判据补到能唯一定位它，
    而不是先建目录、再由后来者反推它凭什么存在。
    """
    actual = sorted(
        path.name
        for path in (APP_ROOT / "startup").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )
    assert actual == ["bindings", "lifecycle", "ports"], (
        f"app/startup/ 的子目录集合变为 {actual}\n"
        "判据 S 只承认 lifecycle/（S1 决定时刻）、bindings/（S3 供消费方读取的绑定表）"
        "与 ports/（S4 组合根构造、别处长期持有的端口实现及其运行时形状）。"
        "新增子目录前先扩充判据 S 并同步更新本断言。"
    )


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


# 路由依赖模块的职责就是声明 FastAPI 依赖链，其对 Depends 与令牌校验的引用属于
# 该职责本身，不是应用层向传输层的越界扩散。
TRANSPORT_AWARE_APPLICATION_FILES = frozenset({
    "app/application/security/dependencies.py",
})


def test_application_does_not_import_transport_frameworks():
    """应用层不得依赖 FastAPI、Starlette 或宿主 HTTP 适配器。"""
    violations: dict[str, set[str]] = {}
    for path in (APP_ROOT / "application").rglob("*.py"):
        if str(path.relative_to(PROJECT_ROOT)) in TRANSPORT_AWARE_APPLICATION_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        forbidden: set[str] = set()
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                candidates.append(node.module)
            forbidden.update(
                candidate
                for candidate in candidates
                if candidate.startswith(
                    ("fastapi", "starlette", "app.api", "app.adapters.web")
                )
            )
        if forbidden:
            violations[str(path.relative_to(PROJECT_ROOT))] = forbidden
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
    `MODULE_VARIANT_DEPENDENCIES` 声明同一产品线变体共用一份客户端实现的继承边，
    它不是跨模块编排，仍不允许出现在该表之外。
    """
    modules = _discover_modules()
    known_modules = set(modules)
    violations: dict[str, set[str]] = {}
    for module_name, path in modules.items():
        if not module_name.startswith("app.modules."):
            continue
        own_package = module_name.split(".")[2]
        allowed = {own_package, "_base", *MODULE_VARIANT_DEPENDENCIES.get(own_package, ())}
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency
            for dependency in dependencies
            if dependency.startswith("app.application.orchestration")
            or (
                dependency.startswith("app.modules.")
                and dependency.split(".")[2] not in allowed
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
    for path in (APP_ROOT / "application" / "orchestration").rglob("*.py"):
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
        if not module_name.startswith("app.application.orchestration"):
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
        if not module_name.startswith("app.application.orchestration"):
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
    "app.application",
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


def _package_of(module_name: str) -> str:
    """取模块所属的顶级包名；``app`` 下的散件归入 ``(root)``。"""
    parts = module_name.split(".")
    if len(parts) < 3:
        return "(root)"
    return parts[1]


def test_package_dependencies_follow_the_layer_matrix():
    """包级依赖必须落在允许矩阵内，未登记为负债的越界一律拒绝。

    文件级无环由强连通分量断言保证；本断言约束方向本身：
    下层不得依赖上层，扩展之间不得互相依赖。
    """
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        source = _package_of(module_name)
        allowed = PACKAGE_LAYERS.get(source)
        if allowed is None:
            continue
        for dependency in dependencies:
            target = _package_of(dependency)
            if target in (source, "(root)") or target in allowed:
                continue
            if (source, target) in DEPENDENCY_DEBT:
                continue
            violations.setdefault(module_name, set()).add(dependency)
    assert violations == {}
