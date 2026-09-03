import ast
import json
from functools import lru_cache
from pathlib import Path

from app.runtime.compat.manifest import MODULE_ALIASES, SYMBOL_ALIASES
from scripts.architecture.baseline import (
    collect_current_event_facts as _collect_current_event_facts,
)
from scripts.architecture.baseline import (
    discover_modules as _discover_modules,
)
from scripts.architecture.baseline import (
    resolve_imports as _resolve_imports,
)
from scripts.architecture.baseline import (
    strongly_connected_components as _strongly_connected_components,
)

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
DEPENDENCY_POLICY_PATH = PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "dependency-policy.json"
LEGACY_ROOTS = ("app.core", "app.helper", "app.utils")
LEGACY_MODULES = {"app.log"}
IMPLEMENTATION_ROOTS = (
    "app.agent.skills",
    "app.adapters",
    "app.application",
    "app.db.adapters",
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
    "app/runtime/dependencies.py",
    "app/runtime/native_dependencies.py",
    "app/agent/runtime_loader.py",
    "app/agent/llm/server_tools.py",
    "app/agent/middleware/activity_log.py",
    "app/agent/middleware/patch_tool_calls.py",
    "app/agent/middleware/runtime_config.py",
    "app/agent/middleware/tool_selection.py",
    "app/agent/policy/secret_fields.py",
    "app/agent/prompt/transfer_redo.py",
    "app/api/openai_utils.py",
    "app/api/router_specs.py",
    "app/adapters/external/wechat_crypt.py",
    "app/modules/_base/media_auxiliary.py",
    "app/modules/indexer/parser/ipt_project.py",
    "app/modules/musicbrainz/music_cache.py",
    "app/modules/themoviedb/tmdb_cache.py",
    "app/modules/thetvdb/tvdb_v4_official.py",
    "app/runtime/compat/resource_imports.py",
    "app/runtime/extensions/host_module_adapter.py",
    "app/runtime/extensions/module_manager.py",
    "app/runtime/extensions/plugin_manager.py",
    "app/runtime/extensions/service_config.py",
    "app/testing/network_guard.py",
    "app/chain/media.py",
    "app/adapters/network/rss.py",
    "app/adapters/network/sites.pyi",
    "app/application/plugins.py",
    "app/application/subscribe.py",
    "app/application/torrent.py",
    "app/application/torrent_cache.py",
    "app/application/chain/durable_events.py",
    "app/application/transfer.py",
    "app/application/transfer_execution.py",
    "app/db/adapters/transfer.py",
    "app/db/adapters/transfer_execution.py",
    "app/runtime/extensions/managed_resource_adapter.py",
    "app/runtime/managed_resources.py",
    "app/startup/agent_initializer.py",
    "app/startup/cache_initializer.py",
    "app/startup/chain_events.py",
    "app/startup/command_initializer.py",
    "app/startup/configuration.py",
    "app/startup/context.py",
    "app/startup/database.py",
    "app/startup/database_initializer.py",
    "app/startup/domain_initializer.py",
    "app/startup/download_failure.py",
    "app/startup/managed_resources_initializer.py",
    "app/startup/initializers/managed_resources.py",
    "app/startup/modules_initializer.py",
    "app/startup/monitor_initializer.py",
    "app/startup/outbox.py",
    "app/startup/plugins_initializer.py",
    "app/startup/routers_initializer.py",
    "app/startup/scheduler_initializer.py",
    "app/startup/site.py",
    "app/startup/subscription.py",
    "app/startup/transaction.py",
    "app/startup/transfer_initializer.py",
    "app/startup/workflow.py",
    "app/startup/workflow_initializer.py",
)
HOST_MODULE_PACKAGE_EXPORTS = {
    "filemanager": {"FileManagerModule"},
    "qqbot": {"QQBotModule"},
    "telegram": {"TelegramModule"},
    "trimemedia": {"TrimeMediaModule"},
    "ugreen": {"UgreenModule"},
}
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
        "app.adapters",
        "app.application",
        "app.sdk",
    ),
    "app.application": (
        "app.runtime.extensions",
        "app.runtime.compat",
        "app.sdk",
    ),
    "app.api": (
        "app.runtime.extensions.plugin.manager",
        "app.runtime.extensions.module.manager",
        "app.scheduler",
    ),
    "app.agent": (
        "app.runtime.extensions.plugin.manager",
        "app.runtime.extensions.module.manager",
    ),
    "app.chain": (
        "app.runtime.extensions.plugin.manager",
        "app.runtime.extensions.module.manager",
        "app.runtime.extensions.module.dispatcher",
    ),
    "app.workflow": (
        "app.runtime.extensions.plugin.manager",
        "app.runtime.extensions.module.manager",
    ),
}


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
            candidate for candidate in candidates if candidate in LEGACY_MODULES or candidate.startswith(LEGACY_ROOTS)
        )
    return imports


def _attribute_parts(node: ast.Attribute) -> list[str]:
    """将静态属性访问还原为从根名称开始的完整路径片段。"""
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return []
    parts.append(value.id)
    return list(reversed(parts))


def _compat_symbol_references(tree: ast.AST) -> set[tuple[int, str]]:
    """收集显式导入或静态属性访问命中的兼容符号。"""
    compatibility_symbols = {
        (module_name, symbol_name) for module_name, symbols in SYMBOL_ALIASES.items() for symbol_name in symbols
    }
    module_bindings: dict[str, str] = {}
    references: set[tuple[int, str]] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".", maxsplit=1)[0]
                module_bindings[binding] = alias.name if alias.asname else binding
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (node.module, alias.name) in compatibility_symbols:
                    references.add((node.lineno, f"{node.module}.{alias.name}"))
                    continue
                binding = alias.asname or alias.name
                module_bindings[binding] = f"{node.module}.{alias.name}"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts = _attribute_parts(node)
        if len(parts) < 2:
            continue
        resolved_root = module_bindings.get(parts[0], parts[0])
        resolved = [*resolved_root.split("."), *parts[1:]]
        module_name = ".".join(resolved[:-1])
        symbol_name = resolved[-1]
        if (module_name, symbol_name) in compatibility_symbols:
            references.add((node.lineno, f"{module_name}.{symbol_name}"))

    return references


def _class_annotations(path: Path, class_name: str) -> dict[str, str]:
    """返回指定类的源码级字段注解。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


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
    leftovers = [root_name for root_name in ("core", "helper", "utils") if (APP_ROOT / root_name).exists()]
    assert leftovers == []


def test_retired_canonical_filenames_do_not_return():
    """能力包应使用包内语境明确的短文件名，避免再次出现冗余角色后缀。"""
    leftovers = [relative_path for relative_path in RETIRED_CANONICAL_FILES if (PROJECT_ROOT / relative_path).exists()]
    assert leftovers == []


def test_agent_manager_uses_focused_owner_modules_and_precise_facade():
    """AgentManager 必须保持薄门面，编排器不得重新聚合 Manager 实现。"""
    owner_paths = {
        "manager": APP_ROOT / "agent" / "manager.py",
        "session": APP_ROOT / "agent" / "session.py",
        "lifecycle": APP_ROOT / "agent" / "lifecycle.py",
        "tasks": APP_ROOT / "agent" / "tasks.py",
        "orchestrator": APP_ROOT / "agent" / "orchestrator.py",
    }
    assert all(path.is_file() for path in owner_paths.values())

    manager_tree = ast.parse(owner_paths["manager"].read_text(encoding="utf-8-sig"))
    manager_class = next(
        node for node in manager_tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentManager"
    )
    manager_methods = {
        node.name for node in manager_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert manager_methods == {"__init__"}
    assert [ast.unparse(base) for base in manager_class.bases] == ["AgentTaskOwner"]

    orchestrator_tree = ast.parse(owner_paths["orchestrator"].read_text(encoding="utf-8-sig"))
    orchestrator_classes = {node.name for node in orchestrator_tree.body if isinstance(node, ast.ClassDef)}
    orchestrator_assignments = {
        target.id
        for node in orchestrator_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "AgentManager" not in orchestrator_classes
    assert "_MessageTask" not in orchestrator_classes
    assert "agent_manager" not in orchestrator_assignments

    startup_source = (APP_ROOT / "startup" / "initializers" / "agent.py").read_text(encoding="utf-8-sig")
    assert "from app.agent.manager import AgentManager" in startup_source
    assert "from app.agent.orchestrator import AgentManager" not in startup_source


def test_runtime_dependencies_use_same_named_single_word_package() -> None:
    """运行依赖能力必须使用同名包，且宿主直接依赖单一职责子模块。"""
    package = APP_ROOT / "runtime" / "dependencies"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "native.py",
        "profile.py",
    }

    init_tree = ast.parse(
        (package / "__init__.py").read_text(encoding="utf-8"),
        filename=str(package / "__init__.py"),
    )
    assert ast.get_docstring(init_tree)
    assert len(init_tree.body) == 1

    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "plugins":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.runtime.dependencies":
                violations.append(f"{relative}:{node.lineno}:from-package-root")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{relative}:{node.lineno}:package-root"
                    for alias in node.names
                    if alias.name == "app.runtime.dependencies"
                )
    assert violations == []


def test_domain_classification_is_a_pure_direct_import_package() -> None:
    """分类领域包根仅承载文档，且实现不得依赖配置、持久化或具体来源。"""
    package = APP_ROOT / "domain" / "classification"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "evaluator.py",
        "facts.py",
        "fields.py",
        "sources.py",
        "validation.py",
    }

    init_path = package / "__init__.py"
    init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    assert ast.get_docstring(init_tree)
    assert len(init_tree.body) == 1

    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core.config",
        "app.db",
        "app.modules",
        "app.runtime",
        "app.sdk",
    )
    violations: list[str] = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            elif isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            violations.extend(
                f"{path.name}:{node.lineno}:{module}"
                for module in imported_modules
                if module.startswith(forbidden_prefixes)
            )
    assert violations == []

    root_imports: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if path.is_relative_to(APP_ROOT / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.domain.classification":
                root_imports.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}")
            elif isinstance(node, ast.Import):
                root_imports.extend(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
                    for alias in node.names
                    if alias.name == "app.domain.classification"
                )
    assert root_imports == []


def test_application_classification_uses_same_named_package() -> None:
    """分类应用能力必须归入同名包，包根不得重复导出内部实现。"""
    package = APP_ROOT / "application" / "classification"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "analysis.py",
        "catalog.py",
        "configuration.py",
        "contract.py",
        "enrichment.py",
        "execution.py",
        "legacy.py",
        "migration.py",
        "projection.py",
        "reference.py",
        "runtime.py",
    }

    init_path = package / "__init__.py"
    init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    assert ast.get_docstring(init_tree)
    assert len(init_tree.body) == 1


def test_application_torrent_uses_same_named_single_word_package() -> None:
    """种子应用能力必须归入同名包，包根不得重复导出内部实现。"""
    package = APP_ROOT / "application" / "torrent"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "cache.py",
        "download.py",
    }
    init_tree = ast.parse(
        (package / "__init__.py").read_text(encoding="utf-8"),
        filename=str(package / "__init__.py"),
    )
    assert ast.get_docstring(init_tree)
    assert len(init_tree.body) == 1

    root_imports: list[str] = []
    for root in (APP_ROOT, PROJECT_ROOT / "database"):
        for path in root.rglob("*.py"):
            if path.is_relative_to(APP_ROOT / "plugins"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "app.application.torrent":
                    root_imports.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}")
    assert root_imports == []


def test_host_module_package_roots_only_export_capability_entrypoints() -> None:
    """宿主模块包根只公开 capability entrypoint，内部 owner 必须直达子模块。"""
    base_path = APP_ROOT / "modules" / "_base" / "__init__.py"
    base_tree = ast.parse(base_path.read_text(encoding="utf-8"), filename=str(base_path))
    assert ast.get_docstring(base_tree)
    assert len(base_tree.body) == 1

    root_imports: list[str] = []
    for path in (APP_ROOT / "modules").rglob("*.py"):
        if path.is_relative_to(APP_ROOT / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.modules._base":
                root_imports.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}")
    assert root_imports == []

    for package_name, expected_exports in HOST_MODULE_PACKAGE_EXPORTS.items():
        path = APP_ROOT / "modules" / package_name / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = {
            target.id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id in {"_EXPORTS", "__all__"}
        }
        assert set(assignments["_EXPORTS"]) == expected_exports
        assert set(assignments["__all__"]) == expected_exports


def test_domain_media_projection_uses_single_word_owner_package() -> None:
    """媒体来源投影必须由单词 owner 完整承接，canonical 模型只保留薄委托。"""
    package = APP_ROOT / "domain" / "projection"
    assert {path.name for path in package.glob("*.py")} == {
        "__init__.py",
        "anilist.py",
        "bangumi.py",
        "douban.py",
        "mapping.py",
        "tmdb.py",
    }
    init_tree = ast.parse(
        (package / "__init__.py").read_text(encoding="utf-8"),
        filename=str(package / "__init__.py"),
    )
    assert ast.get_docstring(init_tree)
    assert len(init_tree.body) == 1

    context_path = APP_ROOT / "domain" / "context.py"
    context_tree = ast.parse(
        context_path.read_text(encoding="utf-8-sig"),
        filename=str(context_path),
    )
    media_class = next(
        node for node in context_tree.body if isinstance(node, ast.ClassDef) and node.name == "MediaInfo"
    )
    methods = {
        node.name: node for node in media_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in (
        "set_tmdb_info",
        "set_douban_info",
        "set_bangumi_info",
        "set_anilist_info",
    ):
        method = methods[name]
        executable = [
            node
            for node in method.body
            if not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        ]
        assert len(executable) == 1
        assert isinstance(executable[0], ast.Expr)
        assert isinstance(executable[0].value, ast.Call)

    schema_source = (APP_ROOT / "schemas" / "context.py").read_text(encoding="utf-8-sig")
    assert "app.domain.projection" not in schema_source
    assert not any(name in schema_source for name in methods if name.startswith("set_") and name.endswith("_info"))

    forbidden_calls: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "plugins" or path == context_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id == "MediaInfo" and node.attr in {
                "get_bangumi_media_type",
                "get_anilist_media_type",
                "_anilist_date",
                "_anilist_chinese_title",
            }:
                forbidden_calls.append(f"{relative}:{node.lineno}:{node.attr}")
    assert forbidden_calls == []


def test_workflow_query_contract_returns_only_typed_snapshots():
    """工作流正式查询端口不得退化为 Any 或 ORM 返回值。"""
    path = APP_ROOT / "application" / "workflow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    query_classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in {"WorkflowQueryRepository", "WorkflowQueryService"}
    }
    methods = [
        node
        for query_class in query_classes.values()
        for node in query_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("__")
        and node.returns is not None
    ]

    assert set(query_classes) == {"WorkflowQueryRepository", "WorkflowQueryService"}
    assert methods
    for method in methods:
        annotation = ast.unparse(method.returns)
        assert "Any" not in annotation
        if method.name in {"count", "async_count"}:
            assert annotation == "int"
        else:
            assert "WorkflowSnapshot" in annotation


def test_workflow_query_consumers_do_not_reach_raw_oper():
    """API、Agent、共享服务和运行时管理器只消费统一快照查询服务。"""
    consumer_paths = (
        "app/api/dependencies/workflow.py",
        "app/application/server/share.py",
        "app/workflow/__init__.py",
    )
    violations = {}
    for relative_path in consumer_paths:
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        forbidden = {
            name
            for name in (
                "WorkflowOper",
                "get_agent_workflow_port",
                "get_chain_workflow_port",
            )
            if name in source
        }
        if forbidden:
            violations[relative_path] = sorted(forbidden)

    assert violations == {}


def test_workflow_query_adapter_owns_projection_sessions():
    """唯一查询适配器必须在自有同步和异步 Session 内投影快照。"""
    path = APP_ROOT / "db" / "adapters" / "workflow.py"
    source = path.read_text(encoding="utf-8")

    assert "class TransactionalWorkflowQueryRepository" in source
    assert "session.close()" in source
    assert "async with self._async_session() as session" in source
    assert "_project_workflow(record)" in source


def test_workflow_execution_chain_uses_single_application_owned_port():
    """工作流 Chain 写端必须只使用 Application owner 的唯一配置入口。"""
    contract_path = APP_ROOT / "application" / "workflow.py"
    contract_tree = ast.parse(
        contract_path.read_text(encoding="utf-8"),
        filename=str(contract_path),
    )
    contract = next(
        node for node in contract_tree.body if isinstance(node, ast.ClassDef) and node.name == "WorkflowExecutionPort"
    )
    methods = {
        node.name: ast.unparse(node.returns)
        for node in contract.body
        if isinstance(node, ast.FunctionDef) and node.returns is not None
    }
    assert methods == {
        "start": "bool",
        "success": "bool",
        "fail": "bool",
        "step": "bool",
        "reset": "bool",
    }

    chain_source = (APP_ROOT / "chain" / "workflow.py").read_text(encoding="utf-8")
    startup_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    composition_source = (APP_ROOT / "startup" / "composition" / "database.py").read_text(encoding="utf-8")
    assert chain_source.count("get_configured_workflow_execution()") == 1
    assert "get_chain_workflow_port" not in chain_source
    assert "configure_workflow_execution(TransactionalWorkflowExecutionService(SessionFactory))" in composition_source
    assert "configure_workflow_execution_composition()" in startup_source
    assert "workflow=lambda:" not in startup_source


def test_chain_runtime_context_owns_typed_repository_instances():
    """Chain 数据能力必须作为明确类型的实例字段进入运行时上下文。"""
    annotations = _class_annotations(
        APP_ROOT / "application" / "chain" / "context.py",
        "ChainRuntimeContext",
    )
    expected = {
        "site_repository": "SiteRepository",
        "subscription_repository": "SubscriptionRepository",
        "download_history_repository": "DownloadHistoryRepository",
        "transfer_history_repository": "TransferHistoryRepository",
        "transfer_admission_repository": "TransferAdmissionRepository",
        "transfer_execution_repository": "TransferExecutionRepository",
        "media_server_repository": "MediaServerRepository",
        "download_failure_repository": "DownloadFailureRepository",
        "user_repository": "ChainUserRepository",
    }
    assert {name: annotations[name] for name in expected} == expected

    chain_base_source = (APP_ROOT / "chain" / "base.py").read_text(encoding="utf-8-sig")
    for field_name in expected:
        assert f"self.{field_name} = context.{field_name}" in chain_base_source

    download_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((APP_ROOT / "chain" / "download").glob("*.py"))
    )
    mediaserver_source = (APP_ROOT / "chain" / "mediaserver.py").read_text(encoding="utf-8")
    application_source = (APP_ROOT / "application" / "mediaserver.py").read_text(encoding="utf-8")
    startup_source = (APP_ROOT / "startup" / "composition" / "chain.py").read_text(encoding="utf-8")
    assert "DownloadFailure = Any" not in download_source
    assert "dboper" not in mediaserver_source
    assert "async def async_get_item_id(" in application_source
    assert "TransactionalMediaServerRepository(SessionFactory)" in startup_source


def test_startup_composes_typed_chain_and_agent_data_contexts():
    """组合根必须一次性构造 Chain 与 Agent 的类型化数据上下文。"""
    context_specs = {
        "ChainRuntimeContext": (
            APP_ROOT / "application" / "chain" / "context.py",
            APP_ROOT / "startup" / "composition" / "chain.py",
            {
                "site_repository",
                "subscription_repository",
                "subscription_mutation_scope",
                "sync_subscription_mutation_scope",
                "subscription_delete_scope",
                "sync_subscription_delete_scope",
                "subscription_completion_scope",
                "download_history_repository",
                "transfer_history_repository",
                "transfer_admission_repository",
                "transfer_execution_repository",
                "media_server_repository",
                "download_failure_repository",
                "user_repository",
            },
        ),
        "AgentDataContext": (
            APP_ROOT / "application" / "agent.py",
            APP_ROOT / "startup" / "composition" / "agent.py",
            {
                "chat",
                "chat_persistence",
                "tasks",
                "users",
                "sites",
                "subscriptions",
                "subscription_mutation_scope",
                "subscription_delete_scope",
                "subscription_history",
                "transfer_history",
                "transfer_execution",
                "download_history",
                "plugin_data",
            },
        ),
    }
    for class_name, (context_path, composition_path, expected_fields) in context_specs.items():
        annotations = _class_annotations(context_path, class_name)
        assert expected_fields <= annotations.keys()
        assert all(
            "Any" not in annotations[field_name]
            and "Oper" not in annotations[field_name]
            and "Callable" not in annotations[field_name]
            for field_name in expected_fields
        )

        composition_tree = ast.parse(
            composition_path.read_text(encoding="utf-8-sig"),
            filename=str(composition_path),
        )
        constructors = [
            node
            for node in ast.walk(composition_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == class_name
        ]
        assert len(constructors) == 1
        values = {keyword.arg: keyword.value for keyword in constructors[0].keywords if keyword.arg is not None}
        assert expected_fields <= values.keys()
        assert all(not isinstance(values[field_name], ast.Lambda) for field_name in expected_fields)


def test_download_history_ports_are_typed_detached_and_canonically_injected():
    """下载历史宿主调用面只能消费冻结快照和显式事务 adapter。"""
    history_path = APP_ROOT / "application" / "history.py"
    history_tree = ast.parse(
        history_path.read_text(encoding="utf-8"),
        filename=str(history_path),
    )
    classes = {node.name: node for node in history_tree.body if isinstance(node, ast.ClassDef)}
    for class_name in (
        "DownloadHistorySnapshot",
        "DownloadFileSnapshot",
        "DownloadHistoryWrite",
        "DownloadFileWrite",
    ):
        decorator = next(
            item
            for item in classes[class_name].decorator_list
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "dataclass"
        )
        keywords = {item.arg: ast.literal_eval(item.value) for item in decorator.keywords}
        assert keywords == {"frozen": True, "slots": True}

    for class_name in ("DownloadHistoryQueryPort", "DownloadHistoryWritePort"):
        annotations = [
            ast.unparse(node.returns)
            for node in classes[class_name].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None
        ]
        assert annotations
        assert all("Any" not in annotation for annotation in annotations)

    chain_annotations = _class_annotations(
        APP_ROOT / "application" / "chain" / "context.py",
        "ChainRuntimeContext",
    )
    agent_annotations = _class_annotations(
        APP_ROOT / "application" / "agent.py",
        "AgentDataContext",
    )
    assert chain_annotations["download_history_repository"] == "DownloadHistoryRepository"
    assert agent_annotations["download_history"] == "DownloadHistoryRepository"

    consumer_paths = (
        *sorted((APP_ROOT / "chain" / "download").glob("*.py")),
        *sorted((APP_ROOT / "chain" / "transfer").glob("*.py")),
        APP_ROOT / "application" / "transfer" / "workflow.py",
    )
    for path in consumer_paths:
        source = path.read_text(encoding="utf-8")
        assert "app.db.oper.downloadhistory" not in source
        assert "DownloadHistory = Any" not in source
        assert "DownloadFiles = Any" not in source

    startup_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    runtime_source = (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(encoding="utf-8")
    assert "TransactionalDownloadHistoryRepository(" not in startup_source
    assert "TransactionalDownloadHistoryRepository(" in runtime_source
    assert "SessionDownloadHistoryRepository" not in startup_source
    assert "SessionDownloadHistoryRepository" in runtime_source
    assert "DownloadHistoryOper" not in startup_source

    adapter_source = (APP_ROOT / "db" / "adapters" / "history" / "download.py").read_text(encoding="utf-8")
    assert "class TransactionalDownloadHistoryRepository" in adapter_source
    assert "class SessionDownloadHistoryRepository" in adapter_source
    assert "_project_history" in adapter_source
    assert "SqlAlchemyUnitOfWork" in adapter_source

    legacy_source = (APP_ROOT / "sdk" / "_legacy" / "transfer.py").read_text(encoding="utf-8")
    assert "download_history: Optional[Any]" in legacy_source


def test_user_configuration_uses_typed_transactional_adapter():
    """用户配置宿主入口只消费类型化端口，旧 Oper 写入口仅承担兼容 ABI。"""
    application_path = APP_ROOT / "application" / "security" / "userconfig.py"
    application_source = application_path.read_text(encoding="utf-8")
    application_tree = ast.parse(application_source, filename=str(application_path))
    repository = next(
        node
        for node in application_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UserConfigurationRepository"
    )
    returns = {
        node.name: ast.unparse(node.returns)
        for node in repository.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None
    }
    assert returns == {
        "get": "JsonData",
        "set": "None",
        "publish_rename": "None",
        "publish_delete": "None",
    }
    assert "Any" not in application_source

    composition_source = (APP_ROOT / "startup" / "composition" / "configuration.py").read_text(encoding="utf-8")
    assert "TransactionalUserConfigurationRepository(SessionFactory)" in composition_source
    assert "UserConfigOper" not in composition_source

    adapter_path = APP_ROOT / "db" / "adapters" / "configuration.py"
    adapter_source = adapter_path.read_text(encoding="utf-8")
    assert "class TransactionalUserConfigurationRepository" in adapter_source
    assert "SqlAlchemyUnitOfWork" in adapter_source
    assert "Any" not in adapter_source

    oper_source = (APP_ROOT / "db" / "oper" / "userconfig.py").read_text(encoding="utf-8")
    assert "def stage_set(" in oper_source
    assert "def set(" in oper_source
    assert "Any" not in oper_source


def test_modules_initializer_delegates_configuration_and_database_composition():
    """模块初始化器只编排配置与数据库组合 API，不再内联其构造细节。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    configuration_source = (APP_ROOT / "startup" / "composition" / "configuration.py").read_text(encoding="utf-8")
    database_source = (APP_ROOT / "startup" / "composition" / "database.py").read_text(encoding="utf-8")
    runtime_source = (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(encoding="utf-8")

    for call in (
        "start_database_runtime()",
        "stop_database_runtime",
        "compose_configuration(",
        "compose_database_services(",
        "publish_database_services(",
        "publish_configuration(",
        "reset_database_services",
        "reset_configuration",
        "configure_database()",
    ):
        assert call in initializer_source
    for detail in (
        "DatabaseWorker()",
        "TransactionalWriteRunner(",
        "PluginPersistenceService(",
        "SqlAlchemyDataQueryAdapter(",
        "RuntimeConfiguration(",
        "RuntimeSettingsService(",
        "TransactionalUserConfigurationRepository(",
    ):
        assert detail not in initializer_source
    assert "stop_database_worker" not in initializer_source
    assert "RuntimeConfiguration(" in configuration_source
    assert "RuntimeSettingsService(" in configuration_source
    assert "TransactionalUserConfigurationRepository(" in configuration_source
    assert "DatabaseWorker()" in database_source
    assert "TransactionalWriteRunner(" in database_source
    assert "PluginPersistenceService(" in database_source
    assert "SqlAlchemyDataQueryAdapter(" in database_source
    assert "reset_transaction_runners()" in database_source
    assert "configure_api_data_runtime" not in initializer_source
    assert "configure_api_data_runtime" not in database_source
    assert "configure_api_data_runtime" in runtime_source


def test_modules_initializer_delegates_network_composition():
    """模块初始化器只调用网络组合 API，不再内联具体 Adapter 组装。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    network_source = (APP_ROOT / "startup" / "composition" / "network.py").read_text(encoding="utf-8")

    assert "configure_application_network_ports()" in initializer_source
    for detail in (
        "_NetworkTestTransportAdapter",
        "_ImageTransportAdapter",
        "_InternalAddressAdapter",
        "_MessageIngressAdapter",
        "NetworkTestService(",
        "configure_image_ports(",
        "configure_message_ingress_port(",
    ):
        assert detail not in initializer_source
        assert detail in network_source


def test_modules_initializer_delegates_outbox_composition():
    """模块初始化器只发布 Outbox dispatcher，不再内联 handler 与存储装配。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    composition_source = (APP_ROOT / "startup" / "composition" / "outbox.py").read_text(encoding="utf-8")
    package_source = (APP_ROOT / "startup" / "composition" / "__init__.py").read_text(encoding="utf-8")

    assert "configure_outbox_dispatcher(build_outbox_dispatcher)" in initializer_source
    for retired_name in ("_build_outbox_handlers", "_build_outbox_dispatcher"):
        assert retired_name not in initializer_source
    for detail in (
        "def build_outbox_handlers()",
        "def build_outbox_dispatcher()",
        "validate_durable_event_handlers(handlers)",
        "SqlAlchemyOutboxDispatchStore(SessionFactory)",
    ):
        assert detail in composition_source
    assert "build_outbox_handlers" not in package_source
    assert "build_outbox_dispatcher" not in package_source


def test_modules_initializer_delegates_server_service_composition():
    """中心服务对象图只由单词型 composition owner 构造，initializer 仅保留调用顺序。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    composition_path = APP_ROOT / "startup" / "composition" / "server.py"
    composition_source = composition_path.read_text(encoding="utf-8")
    architecture_source = (PROJECT_ROOT / "docs" / "rules" / "05-architecture.md").read_text(encoding="utf-8")
    package_tree = ast.parse((APP_ROOT / "startup" / "composition" / "__init__.py").read_text(encoding="utf-8"))

    assert composition_path.is_file()
    assert not list(composition_path.parent.glob("server_*.py"))
    assert "from app.startup.composition.server import configure_server_services" in (initializer_source)
    assert "configure_server_services(workflow_query, runtime_dependencies.subscription)" in initializer_source
    assert "`startup/composition/server.py` owns MoviePilot Server" in (architecture_source)
    for detail in (
        "ServerReportService(",
        "ServerSharingService(",
        "configure_server_application_services(",
    ):
        assert detail not in initializer_source
        assert detail in composition_source
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)) for node in package_tree.body
    )


def test_modules_initializer_delegates_agent_composition():
    """模块初始化器只编排 Agent composition，不再内联数据与任务构造。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    agent_source = (APP_ROOT / "startup" / "composition" / "agent.py").read_text(encoding="utf-8")
    runtime_source = (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(encoding="utf-8")

    assert "compose_agent(" in initializer_source
    assert "publish_agent_services(" in initializer_source
    assert initializer_source.index("await start_database_runtime()") < initializer_source.index("compose_agent(")
    for detail in (
        "AgentDataContext(",
        "AgentChatPersistenceService(",
        "AgentChatService(",
        "TransactionalAgentTaskRepository(",
        "AgentTaskExecutionService(",
        "configure_agent_chat_service(",
        "configure_agent_chat_persistence(",
        "configure_agent_task_execution(",
    ):
        assert detail not in initializer_source
        assert detail in agent_source
    assert "AgentChatRuntime(" not in initializer_source
    assert "AgentChatRuntime(" not in agent_source
    assert "AgentChatRuntime(" in runtime_source
    assert "runtime.worker.snapshot().capacity" in agent_source
    assert "startup.initializers" not in agent_source


def test_runtime_composition_is_the_sole_host_and_domain_runtime_owner():
    """HostRuntime、领域 Runtime 与 ApiData 投影只能由单词型 runtime owner 构造。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    runtime_path = APP_ROOT / "startup" / "composition" / "runtime.py"
    runtime_source = runtime_path.read_text(encoding="utf-8")
    database_source = (APP_ROOT / "startup" / "composition" / "database.py").read_text(encoding="utf-8")
    package_tree = ast.parse((APP_ROOT / "startup" / "composition" / "__init__.py").read_text(encoding="utf-8"))

    assert runtime_path.is_file()
    assert not list(runtime_path.parent.glob("runtime_*.py"))
    assert "compose_runtime_dependencies()" in initializer_source
    assert "compose_runtime(" in initializer_source
    assert "RuntimeInputs(" in initializer_source
    assert "publish_runtime(runtime_composition)" in initializer_source
    assert "HostRuntime(" not in initializer_source
    assert "ApiDataPorts(" not in initializer_source
    assert "ApiDataPorts(" not in database_source
    for detail in (
        "class RuntimeDependencies:",
        "class RuntimeInputs:",
        "class RuntimeComposition:",
        "HostRuntime(",
        "AgentChatRuntime(",
        "AuthenticationRuntime(",
        "PersistenceRuntime(",
        "MessagingRuntime(",
        "HistoryRuntime(",
        "SiteRuntime(",
        "SubscriptionRuntime(",
        "WorkflowRuntime(",
        "ApiDataPorts(",
    ):
        assert detail in runtime_source
    constructors = []
    for path in APP_ROOT.rglob("*.py"):
        if path.is_relative_to(APP_ROOT / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HostRuntime"
            for node in ast.walk(tree)
        ):
            constructors.append(path.relative_to(APP_ROOT).as_posix())
    assert constructors == ["startup/composition/runtime.py"]
    context_tree = ast.parse((APP_ROOT / "startup" / "composition" / "context.py").read_text(encoding="utf-8"))
    host_runtime = next(
        node for node in context_tree.body if isinstance(node, ast.ClassDef) and node.name == "HostRuntime"
    )
    task_field = next(
        node
        for node in host_runtime.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "tasks"
    )
    assert task_field.value is None
    assert (
        _class_annotations(
            APP_ROOT / "startup" / "composition" / "context.py",
            "AuthenticationRuntime",
        )["passkey_repository"]
        == "RepositoryFactory"
    )
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)) for node in package_tree.body
    )


def test_modules_initializer_delegates_chain_composition():
    """Chain 上下文、壁纸与旧整理命令只由单词型 composition owner 装配。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8")
    composition_path = APP_ROOT / "startup" / "composition" / "chain.py"
    composition_source = composition_path.read_text(encoding="utf-8")
    composition_tree = ast.parse(composition_source, filename=str(composition_path))
    package_tree = ast.parse((APP_ROOT / "startup" / "composition" / "__init__.py").read_text(encoding="utf-8"))
    architecture_source = (PROJECT_ROOT / "docs" / "rules" / "05-architecture.md").read_text(encoding="utf-8")
    runtime_source = (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(encoding="utf-8")

    assert composition_path.is_file()
    assert not list(composition_path.parent.glob("chain_*.py"))
    assert "from app.startup.composition.chain import (" in initializer_source
    assert initializer_source.index("configure_wallpaper_services()") < (
        initializer_source.index("configure_chain_runtime_context(")
    )
    for retired_name in (
        "_execute_legacy_transfer_command",
        "_build_chain_runtime_context",
    ):
        assert retired_name not in initializer_source
    for detail in (
        "ChainRuntimeContext(",
        "TransactionalTransferAdmissionRepository(SessionFactory)",
        "TransactionalChainDurableEventWriter(SessionFactory)",
        "def execute_legacy_transfer_command(",
        "def build_chain_runtime_context(",
        "def configure_wallpaper_services(",
        "def configure_chain_runtime_context(",
    ):
        assert detail not in initializer_source
        assert detail in composition_source
    assert "TransactionalTransferExecutionRepository(" not in initializer_source
    assert runtime_source.count("TransactionalTransferExecutionRepository(") == 1
    assert "dependencies=runtime_dependencies" in initializer_source
    assert "transfer_execution_repository=dependencies.transfer_execution" in composition_source
    assert "TransactionalTransferExecutionRepository" not in composition_source
    transfer_function = next(
        node
        for node in composition_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_legacy_transfer_command"
    )
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "app.chain.transfer.facade"
        for node in ast.walk(transfer_function)
    )
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "app.chain.transfer.facade"
        for node in composition_tree.body
    )
    assert "`startup/composition/chain.py` owns" in architecture_source
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)) for node in package_tree.body
    )


def test_canonical_workflow_oper_has_no_legacy_writer_or_duplicate_exports():
    """工作流旧写入口只能存在于 SDK Legacy facade。"""
    oper_path = APP_ROOT / "db" / "oper" / "workflow.py"
    oper_tree = ast.parse(
        oper_path.read_text(encoding="utf-8"),
        filename=str(oper_path),
    )
    oper_class = next(node for node in oper_tree.body if isinstance(node, ast.ClassDef) and node.name == "WorkflowOper")
    method_names = {node.name for node in oper_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"start", "success", "fail", "step", "reset"}.isdisjoint(method_names)
    assert "legacy" not in oper_path.read_text(encoding="utf-8").lower()

    package_source = (APP_ROOT / "db" / "oper" / "__init__.py").read_text(encoding="utf-8")
    assert '"WorkflowOper"' not in package_source


def test_agent_data_ports_do_not_duplicate_workflow_query_capability():
    """Agent 显式数据上下文不得重复拥有工作流查询能力。"""
    annotations = _class_annotations(
        APP_ROOT / "application" / "agent.py",
        "AgentDataContext",
    )

    assert "workflow" not in annotations
    assert "workflow_query" not in annotations


def test_host_uses_canonical_workflow_manager_name():
    """宿主代码不得继续定义或导入旧 WorkFlowManager 拼写。"""
    violations = []
    for path in APP_ROOT.rglob("*.py"):
        relative_path = path.relative_to(APP_ROOT)
        if relative_path.parts[:2] == ("runtime", "compat"):
            continue
        if "WorkFlowManager" in path.read_text(encoding="utf-8"):
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_startup_root_contains_only_composition_packages():
    """组合根顶层只保留稳定分区，禁止再次堆叠扁平实现文件。"""
    startup_root = APP_ROOT / "startup"
    root_modules = sorted(path.name for path in startup_root.glob("*.py"))
    python_packages = sorted(path.name for path in startup_root.iterdir() if path.is_dir() and any(path.rglob("*.py")))

    assert root_modules == ["__init__.py"]
    assert python_packages == ["composition", "initializers", "lifecycle"]


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


def test_compat_symbol_scanner_covers_static_import_shapes() -> None:
    """兼容符号扫描必须覆盖显式导入、模块别名和完整属性链。"""
    tree = ast.parse(
        """
from app.schemas import TransferTask
import app.schemas as schema_alias
schema_alias.TransferQueue
import app.schemas
app.schemas.TransferTask
from app.application import transfer as transfer_package
transfer_package.TransferQueue
"""
    )

    assert _compat_symbol_references(tree) == {
        (2, "app.schemas.TransferTask"),
        (4, "app.schemas.TransferQueue"),
        (6, "app.schemas.TransferTask"),
        (8, "app.application.transfer.TransferQueue"),
    }


def test_host_code_does_not_use_compat_symbol_aliases() -> None:
    """宿主必须导入 canonical 符号，不得反向消费插件兼容覆盖。"""
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if (
            relative.parts[0] == "plugins"
            or relative.parts[:2] == ("runtime", "compat")
            or relative.parts[:2] == ("sdk", "_legacy")
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        violations.extend(
            f"{relative.as_posix()}:{line}:{symbol}" for line, symbol in sorted(_compat_symbol_references(tree))
        )

    assert violations == []


def test_host_code_uses_explicit_runtime_facade_getters():
    """宿主消费者必须显式调用 getter，不得把兼容 Facade 当作新代码入口。"""
    forbidden_imports = {
        "app.application.module": {"ModuleManager"},
        "app.application.scheduling": {"Scheduler"},
    }
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "plugins" or relative.parts[:2] == (
            "runtime",
            "compat",
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
                if "SystemConfigOper" in target_names and node.value.id == "get_configured_system_config":
                    violations.append(f"{relative.as_posix()}:{node.lineno}:SystemConfigOper")
                continue
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            forbidden_names = forbidden_imports.get(node.module, set())
            for alias in node.names:
                class_shaped_plugin_getter = (
                    node.module == "app.application.plugin.runtime"
                    and alias.name == "get_plugin_manager"
                    and alias.asname is not None
                )
                class_shaped_config_getter = (
                    node.module == "app.application.configuration"
                    and alias.name == "get_configured_system_config"
                    and alias.asname is not None
                )
                if alias.name in forbidden_names or class_shaped_plugin_getter or class_shaped_config_getter:
                    imported_name = alias.asname or alias.name
                    violations.append(f"{relative.as_posix()}:{node.lineno}:{imported_name}")

    assert violations == []


def test_user_chain_and_agent_ports_are_typed_and_orm_free():
    """用户 Chain、Agent 与宿主模块只能消费 Application 用户端口。"""
    chain_annotations = _class_annotations(
        APP_ROOT / "application" / "chain" / "context.py",
        "ChainRuntimeContext",
    )
    agent_annotations = _class_annotations(
        APP_ROOT / "application" / "agent.py",
        "AgentDataContext",
    )
    assert chain_annotations["user_repository"] == "ChainUserRepository"
    assert agent_annotations["users"] == "ChainUserRepository"

    production_paths = [
        APP_ROOT / "chain" / "user.py",
        APP_ROOT / "chain" / "interaction.py",
        APP_ROOT / "chain" / "_messaging.py",
        APP_ROOT / "agent" / "orchestrator.py",
        APP_ROOT / "modules" / "feishu" / "feishu.py",
    ]
    violations: list[str] = []
    for path in production_paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "UserOper":
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:class")
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.oper.user":
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:import")

    assert violations == []


def test_startup_injects_user_adapter_instead_of_raw_oper():
    """启动组合根不得把无会话 UserOper 注入宿主查询调用面。"""
    initializer_source = (APP_ROOT / "startup" / "initializers" / "modules.py").read_text(encoding="utf-8-sig")
    database_source = (APP_ROOT / "startup" / "composition" / "database.py").read_text(encoding="utf-8-sig")
    chain_source = (APP_ROOT / "startup" / "composition" / "chain.py").read_text(encoding="utf-8-sig")
    runtime_source = (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(encoding="utf-8-sig")
    security_source = (APP_ROOT / "startup" / "composition" / "security.py").read_text(encoding="utf-8-sig")
    source = initializer_source + database_source + chain_source + runtime_source + security_source

    assert "TransactionalUserRepository" in source
    assert "SqlAlchemyUserRepository" in source
    assert "build_transactional_user_repository" not in initializer_source
    assert "build_transactional_user_repository" in chain_source
    assert "from app.db.oper.user import UserOper" not in source
    assert "user=lambda: UserOper()" not in source


def test_transfer_pending_oper_import_is_confined_to_database_boundary():
    """宿主仅允许事务适配器和兼容导出直接导入整理待处理 Oper。"""
    allowed_paths = {
        "app/db/adapters/transfer/admission.py",
        "app/db/adapters/chain.py",
        "app/db/adapters/transfer/execution.py",
        "app/db/oper/__init__.py",
    }
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith("app/plugins/") or relative in allowed_paths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "app.db.oper.transferpending" for alias in node.names):
                    violations.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (
                node.module == "app.db.oper.transferpending"
                or (
                    node.module == "app.db.oper"
                    and any(alias.name in {"transferpending", "TransferPendingOper"} for alias in node.names)
                )
            ):
                violations.append(f"{relative}:{node.lineno}")

    assert violations == []


def test_startup_injects_transactional_transfer_admission_repository():
    """启动组合根必须向 Chain 注入事务型整理准入仓储。"""
    path = APP_ROOT / "startup" / "composition" / "chain.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports_repository = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.db.adapters.transfer.admission"
        and any(alias.name == "TransactionalTransferAdmissionRepository" for alias in node.names)
        for node in ast.walk(tree)
    )
    chain_context_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChainRuntimeContext"
    ]

    assert imports_repository is True
    assert len(chain_context_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in chain_context_calls[0].keywords if keyword.arg is not None}
    admission = keywords["transfer_admission_repository"]
    assert isinstance(admission, ast.Call)
    assert isinstance(admission.func, ast.Name)
    assert admission.func.id == "TransactionalTransferAdmissionRepository"


def test_transfer_admission_chain_context_field_is_typed():
    """整理准入仓储必须以明确 Protocol 注入 Chain 上下文。"""
    annotations = _class_annotations(
        APP_ROOT / "application" / "chain" / "context.py",
        "ChainRuntimeContext",
    )

    assert annotations["transfer_admission_repository"] == "TransferAdmissionRepository"


def test_scheduler_does_not_depend_on_database_implementation():
    """Scheduler 只能消费应用端口，不得重新直连 app.db 实现。"""
    dependencies = set().union(
        *(
            module_dependencies
            for module_name, module_dependencies in _build_module_graph().items()
            if module_name == "app.scheduler" or module_name.startswith("app.scheduler.")
        )
    )
    assert {
        dependency for dependency in dependencies if dependency == "app.db" or dependency.startswith("app.db.")
    } == set()


def test_monitor_dispatcher_uses_explicit_history_port_getter():
    """监控分发器不得把兼容 TransferHistoryPort 伪装成数据库 Oper。"""
    path = APP_ROOT / "monitor" / "dispatcher.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.history"
        and any(alias.name == "TransferHistoryPort" for alias in node.names)
    ]

    assert violations == []


def test_canonical_service_config_consumers_use_application_directory():
    """Chain、API、Scheduler 与 Agent 不得绕过命名应用目录读取服务配置。"""
    paths = [
        APP_ROOT / "chain" / "_messaging.py",
        APP_ROOT / "chain" / "mediaserver.py",
        APP_ROOT / "api" / "endpoints" / "message.py",
        *(APP_ROOT / "scheduler").glob("*.py"),
        APP_ROOT / "agent" / "llm" / "capability.py",
        APP_ROOT / "agent" / "tools" / "base.py",
        APP_ROOT / "api" / "endpoints" / "mediaserver.py",
    ]
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.runtime.extensions.service":
                violations.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}")
            if (
                path.name == "mediaserver.py"
                and isinstance(node, ast.ImportFrom)
                and node.module == "app.application.mediaserver"
                and any(alias.name == "MediaServerHelper" for alias in node.names)
            ):
                violations.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:MediaServerHelper")

    assert violations == []


def test_plugin_components_do_not_reexport_legacy_abi_names():
    """插件组件只在 manager owner 定义 PluginManager，不得复制旧 ABI。"""
    violations: list[str] = []
    manager_path = APP_ROOT / "runtime" / "extensions" / "plugin" / "manager.py"
    for root in PLUGIN_COMPONENT_ROOTS:
        for path in (PROJECT_ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == "__getattr__" or node.name in PLUGIN_LEGACY_ABI_NAMES:
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.name}")
                elif isinstance(node, ast.ClassDef):
                    is_canonical_manager = path == manager_path and node.name == "PluginManager"
                    if not is_canonical_manager and (
                        node.name in PLUGIN_LEGACY_ABI_NAMES or node.name.endswith("Oper")
                    ):
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.name}")
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        is_legacy_name = alias.name in PLUGIN_LEGACY_ABI_NAMES or alias.name.endswith("Oper")
                        is_private = bool(alias.asname and alias.asname.startswith("_"))
                        if is_legacy_name and not is_private:
                            violations.append(f"{path.relative_to(PROJECT_ROOT)}:{alias.name}")
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = {target.id for target in targets if isinstance(target, ast.Name)}
                    forbidden = {
                        name
                        for name in names
                        if name == "__all__" or name in PLUGIN_LEGACY_ABI_NAMES or name.endswith("Oper")
                    }
                    violations.extend(f"{path.relative_to(PROJECT_ROOT)}:{name}" for name in sorted(forbidden))
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
            isinstance(node, ast.ImportFrom) and node.module in {"app.db", "app.db.models"} for node in ast.walk(tree)
        ):
            violations.append(str(path.relative_to(PROJECT_ROOT)))
    assert violations == []


def test_database_opers_use_dboper_transaction_dispatchers():
    """Oper 不得绕过 DbOper 的统一 Session 类型分派直接调用事务 runner。"""
    runner_names = {"run_sync_transaction", "run_async_transaction"}
    violations: list[str] = []
    for path in (APP_ROOT / "db" / "oper").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "app.db.uow":
                continue
            imported = {alias.name for alias in node.names} & runner_names
            if imported:
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                violations.append(f"{relative}:{node.lineno}:{','.join(sorted(imported))}")

    assert violations == []


def test_models_and_base_require_explicit_database_sessions():
    """Model/Base 不得装饰事务，且所有 db 参数必须由调用方显式传入。"""
    decorator_names = {
        "db_query",
        "db_update",
        "async_db_query",
        "async_db_update",
        "legacy_db_query",
        "legacy_db_update",
        "legacy_async_db_query",
        "legacy_async_db_update",
    }
    violations: list[str] = []
    paths = [APP_ROOT / "db" / "base.py"]
    paths.extend((APP_ROOT / "db" / "models").rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = str(path.relative_to(PROJECT_ROOT))
        nodes = list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.decorators":
                violations.append(f"{relative}:{node.lineno}:decorator-import")
        if path.name == "base.py":
            base_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Base")
            nodes = list(ast.walk(base_class))
        for node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                name = (
                    decorator.id
                    if isinstance(decorator, ast.Name)
                    else decorator.attr
                    if isinstance(decorator, ast.Attribute)
                    else None
                )
                if name in decorator_names:
                    violations.append(f"{relative}:{node.lineno}:@{name}")
            arguments = [*node.args.posonlyargs, *node.args.args]
            defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(node.args.defaults)
            for argument, default in zip(arguments, defaults):
                if argument.arg != "db":
                    continue
                annotation = ast.unparse(argument.annotation) if argument.annotation else ""
                if default is not None or "None" in annotation:
                    violations.append(f"{relative}:{node.lineno}:{node.name}:optional-db")
    assert violations == []


def test_plugin_sdk_does_not_import_or_export_host_models():
    """插件 SDK 不得暴露 ORM Model，只有精确旧 ABI 门面可在内部访问。"""
    internal_compat_imports = {
        ("app/sdk/_legacy/transferpending.py", "app.db.models.transferpending"),
    }
    violations: list[str] = []
    for path in (APP_ROOT / "sdk").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "app.db.models" or node.module.startswith("app.db.models."))
                and (relative, node.module) not in internal_compat_imports
            ):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{node.module}")
    assert violations == []


def test_entry_layers_do_not_import_database_implementations():
    """API、应用、编排、Agent、监控、模块和 Runtime 只能经端口访问持久化。"""
    graph = _build_module_graph()
    layer_roots = (
        "app.api",
        "app.application",
        "app.agent",
        "app.chain",
        "app.monitor",
        "app.modules",
        "app.runtime",
        "app.workflow",
        "app.adapters",
    )
    violations = {
        source: sorted(dependency for dependency in dependencies if dependency.startswith("app.db"))
        for source, dependencies in graph.items()
        if source.startswith(layer_roots) and any(dependency.startswith("app.db") for dependency in dependencies)
    }
    assert violations == {}


def test_migrated_modules_are_not_in_import_cycles():
    """任何 canonical 迁移模块都不得进入完整应用依赖图的环。"""
    modules = _discover_modules()
    known_modules = set(modules)
    graph = {name: _resolve_imports(name, path, known_modules) for name, path in modules.items()}
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
            dependency for dependency in dependencies if dependency.startswith(("app.sdk", "app.runtime.compat"))
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
            (root for root in FORBIDDEN_IMPORT_PREFIXES if module_name == root or module_name.startswith(f"{root}.")),
            None,
        )
        if not source_root:
            continue
        dependencies = _resolve_imports(module_name, path, known_modules)
        forbidden = {
            dependency for dependency in dependencies if dependency.startswith(FORBIDDEN_IMPORT_PREFIXES[source_root])
        }
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_application_does_not_import_transport_frameworks():
    """应用层不得依赖 FastAPI、Starlette 或宿主 HTTP 适配器。"""
    violations: dict[str, set[str]] = {}
    for path in (APP_ROOT / "application").rglob("*.py"):
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
                if candidate.startswith(("fastapi", "starlette", "app.api", "app.adapters.web"))
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


def test_host_code_does_not_import_chain_plugin_abi_roots() -> None:
    """Subscribe/Transfer 包根只服务插件 ABI，宿主必须直接导入规范 Facade。"""
    compatibility_roots = {"app.chain.subscribe", "app.chain.transfer"}
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] == "plugins":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in compatibility_roots:
                violations.append(f"{relative}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{relative}:{node.lineno}:{alias.name}"
                    for alias in node.names
                    if alias.name in compatibility_roots
                )

    assert violations == []


def test_runtime_log_is_a_dependency_leaf():
    """底层可引用运行时日志，但日志模块本身不得反向导入应用模块。"""
    modules = _discover_modules()
    dependencies = _resolve_imports(
        "app.runtime.log",
        modules["app.runtime.log"],
        set(modules),
    )
    assert {dependency for dependency in dependencies if dependency.startswith("app.")} == set()


def test_foundation_does_not_emit_runtime_logs():
    """基础机制不打印或初始化日志系统，运行期诊断由上层调用方负责。"""
    violations: list[str] = []
    for path in (APP_ROOT / "foundation").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "logging" for alias in node.names):
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
            if isinstance(node, ast.ImportFrom) and node.module in {
                "logging",
                "app.runtime.log",
            }:
                violations.append(str(path.relative_to(PROJECT_ROOT)))
                break
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
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
    assert {dependency for dependency in dependencies if dependency.startswith("app.adapters.cache")} == set()


def test_passkey_application_does_not_select_cache_backend():
    """PassKey 用例只消费原子缓存端口，不得识别 Redis 或后端类型。"""
    modules = _discover_modules()
    path = modules["app.application.security.passkey"]
    dependencies = _resolve_imports(
        "app.application.security.passkey",
        path,
        set(modules),
    )
    source = path.read_text(encoding="utf-8-sig")

    assert {dependency for dependency in dependencies if dependency.startswith("app.adapters.cache")} == set()
    assert "RedisHelper" not in source
    assert ".is_redis(" not in source


def test_startup_explicitly_configures_passkey_challenge_cache():
    """PassKey challenge 缓存必须由启动组合根显式装配。"""
    path = APP_ROOT / "startup" / "composition" / "security.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    configured = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "configure_passkey_challenge_cache"
        for node in ast.walk(tree)
    )

    assert configured is True


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
            or (dependency.startswith("app.modules.") and dependency.split(".")[2] not in (own_package, "_base"))
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
        forbidden = {dependency for dependency in dependencies if dependency.startswith("app.modules.")}
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
        forbidden = {dependency for dependency in dependencies if dependency.startswith("app.modules.")}
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
    return {name: _resolve_imports(name, path, known_modules) for name, path in modules.items()}


def _load_dependency_policy() -> dict:
    """读取人工审查的依赖语义 policy；生成基线不得改写该文件。"""
    return json.loads(DEPENDENCY_POLICY_PATH.read_text(encoding="utf-8"))


def _scc_policy_violations(
    graph: dict[str, set[str]],
    entries: list[dict],
) -> tuple[list[list[str]], list[list[str]]]:
    """返回未审查 SCC 与已经失效但未清理的 policy SCC。"""
    actual = {tuple(component) for component in _strongly_connected_components(graph)}
    reviewed = {tuple(entry["modules"]) for entry in entries}
    return (
        [list(component) for component in sorted(actual - reviewed)],
        [list(component) for component in sorted(reviewed - actual)],
    )


def test_complete_host_sccs_match_reviewed_policy() -> None:
    """完整宿主图的每个 SCC 都必须精确匹配人工 policy，且不得保留陈旧项。"""
    policy = _load_dependency_policy()
    entries = policy["allowed_sccs"]

    assert policy["schema_version"] == 3
    assert policy["scope"] == {
        "dependency_kind": "static_runtime_imports",
        "excluded_roots": ["app/plugins"],
        "include_parent_package_initialization": True,
        "root": "app",
    }
    assert {entry["classification"] for entry in entries} == {"contained_vendor"}
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(entry["modules"] == sorted(set(entry["modules"])) for entry in entries)
    assert all(entry["reason"] and entry["tracking"] for entry in entries)
    all_members = [member for entry in entries for member in entry["modules"]]
    assert len(all_members) == len(set(all_members))

    vendor = next(entry for entry in entries if entry["classification"] == "contained_vendor")
    assert vendor["id"] == "themoviedb-vendored-package"
    assert vendor["tracking"] == "replace-or-upgrade-vendored-package"
    assert len(vendor["modules"]) == 29
    assert all(member.startswith("app.modules.themoviedb") for member in vendor["modules"])

    unreviewed, stale = _scc_policy_violations(_build_module_graph(), entries)
    assert unreviewed == []
    assert stale == []


def test_scc_policy_rejects_unreviewed_cycles_in_every_host_root() -> None:
    """API、Chain、Module 与 Startup 内的新环都必须落入未审查集合。"""
    for root in ("app.api", "app.chain", "app.modules", "app.startup"):
        first = f"{root}.first"
        second = f"{root}.second"
        unreviewed, stale = _scc_policy_violations(
            {first: {second}, second: {first}},
            [],
        )
        assert unreviewed == [[first, second]]
        assert stale == []


def test_scc_policy_rejects_changed_or_stale_membership() -> None:
    """已知 SCC 缩小、扩大或消失后必须同步审查 policy，不能静默放行。"""
    entries = [{"modules": ["app.chain.first", "app.chain.second", "app.chain.third"]}]
    unreviewed, stale = _scc_policy_violations(
        {
            "app.chain.first": {"app.chain.second"},
            "app.chain.second": {"app.chain.first"},
            "app.chain.third": set(),
        },
        entries,
    )
    assert unreviewed == [["app.chain.first", "app.chain.second"]]
    assert stale == [["app.chain.first", "app.chain.second", "app.chain.third"]]


def test_contained_vendor_scc_may_have_one_way_outbound_dependency() -> None:
    """vendor SCC 的普通单向向下依赖不应把包外模块误纳入 containment。"""
    first = "app.modules.themoviedb.first"
    second = "app.modules.themoviedb.second"
    domain = "app.domain.media"
    entries = [{"modules": [first, second]}]
    unreviewed, stale = _scc_policy_violations(
        {first: {second, domain}, second: {first}, domain: set()},
        entries,
    )
    assert unreviewed == []
    assert stale == []


def test_host_dependency_graph_excludes_plugin_copies() -> None:
    """`app/plugins/**` 是运行时副本，不能进入宿主模块或 SCC 图。"""
    assert all(not module.startswith("app.plugins") for module in _discover_modules())


def test_chain_does_not_import_agent_implementation():
    """编排层不得反向依赖 Agent 实现，跨域编排经 application 门面。"""
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        if not module_name.startswith("app.chain"):
            continue
        forbidden = {dependency for dependency in dependencies if dependency.startswith("app.agent")}
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_agent_application_facade_does_not_import_agent_implementation():
    """Agent application 门面只能接收组合根注入，不能反向解析具体实现。"""
    dependencies = _build_module_graph()["app.application.agent"]
    assert {dependency for dependency in dependencies if dependency.startswith("app.agent")} == set()


def test_host_consumers_get_agent_manager_through_application_facade():
    """宿主消费者不得绕过 Application 门面直接定位 Agent manager。"""
    forbidden = {"get_agent_manager", "get_running_agent_manager"}
    violations: dict[str, set[str]] = {}
    for module_name, path in _discover_modules().items():
        if module_name.startswith(("app.agent", "app.startup")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.agent.loader"
            for alias in node.names
            if alias.name in forbidden
        }
        if imported:
            violations[module_name] = imported
    assert violations == {}


def test_agent_package_roots_do_not_duplicate_implementation_exports():
    """Agent 与 LLM 包根不得实现动态转发，旧符号只能由精确 Compat 路由承接。"""
    for relative_path in ("app/agent/__init__.py", "app/agent/llm/__init__.py"):
        path = PROJECT_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert all(isinstance(node, ast.Expr) for node in tree.body), relative_path

    assert set(SYMBOL_ALIASES["app.agent"]) == {
        "AgentChain",
        "AgentManager",
        "HEARTBEAT_SESSION_PREFIX",
        "MoviePilotAgent",
        "ReplyMode",
        "UNSUPPORTED_IMAGE_INPUT_MESSAGE",
        "agent_manager",
    }
    assert set(SYMBOL_ALIASES["app.agent.llm"]) == {"LLMHelper"}
    helper_alias = MODULE_ALIASES["app.helper.llm"]
    assert helper_alias.target == "app.agent.llm.helper"
    assert helper_alias.replacement == "app.agent.llm.helper"
    assert not helper_alias.is_package


def test_host_code_imports_agent_and_llm_symbols_from_owner_modules():
    """宿主不得消费 Agent 包根兼容符号，避免包根再次成为第二公开面。"""
    violations: dict[str, set[str]] = {}
    for module_name, path in _discover_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            f"{node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in {"app.agent", "app.agent.llm"}
            for alias in node.names
        }
        if imported:
            violations[module_name] = imported
    assert violations == {}


def test_host_consumers_resolve_llm_provider_runtime_through_gateway():
    """宿主不得绕过 gateway 或 LLM 公共导出穿透 provider 实现。"""
    violations: dict[str, set[str]] = {}
    graph = _build_module_graph()
    for module_name, path in _discover_modules().items():
        if module_name.startswith(("app.agent", "app.startup")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in {"app.agent.llm", "app.agent.llm.provider"}
            for alias in node.names
            if alias.name == "LLMProviderManager"
        }
        forbidden = set(imported)
        if "app.agent.llm.provider" in graph[module_name]:
            forbidden.add("app.agent.llm.provider")
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_host_consumers_use_agent_audio_capability_application_port():
    """宿主消费者不得绕过 Application 门面直连 Agent 音频实现。"""
    violations: dict[str, set[str]] = {}
    for module_name, path in _discover_modules().items():
        if module_name.startswith(("app.agent", "app.startup")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in {"app.agent.llm", "app.agent.llm.capability"}
            for alias in node.names
            if alias.name == "AgentCapabilityManager"
        }
        if imported:
            violations[module_name] = imported
    assert violations == {}


def test_application_services_do_not_resolve_event_manager_singleton():
    """Application 服务必须接收事件端口，不得自行定位进程级事件单例。"""
    violations = {
        module_name: dependencies & {"app.runtime.events"}
        for module_name, dependencies in _build_module_graph().items()
        if module_name.startswith("app.application") and "app.runtime.events" in dependencies
    }
    assert violations == {}


def test_http_endpoints_do_not_register_process_event_listeners():
    """HTTP 端点不得拥有进程级事件监听器，监听装配必须留在 startup。"""
    facts = _collect_current_event_facts()
    callers = {item["caller"] for item in facts["consumers"]}

    assert {caller for caller in callers if caller.startswith("app.api")} == set()


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


def test_modules_read_deployment_settings_through_runtime_port():
    """宿主 Module 不得绕过 runtime 配置端口直接依赖 Settings 实例。"""
    violations: list[str] = []
    for path in (APP_ROOT / "modules").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "app.runtime.config":
                continue
            if any(alias.name == "settings" for alias in node.names):
                violations.append(path.relative_to(PROJECT_ROOT).as_posix())
                break

    assert violations == []


def test_runtime_implementation_does_not_use_legacy_settings_proxy():
    """runtime 实现只能使用只读配置端口，不得重新引入迁移期代理对象。"""
    violations: list[str] = []
    for path in (APP_ROOT / "runtime").rglob("*.py"):
        if path == APP_ROOT / "runtime" / "settings.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.runtime.settings":
                continue
            if any(alias.name.lower().endswith("compat") for alias in node.names):
                violations.append(path.relative_to(PROJECT_ROOT).as_posix())
                break

    assert violations == []


def test_deprecated_settings_proxy_imports_are_zero():
    """宿主代码不得导入已删除的 Settings 兼容代理。"""
    limits = {
        "adapters": 0,
        "agent": 0,
        "application": 0,
        "cli.py": 0,
        "doctor": 0,
        "factory.py": 0,
        "main.py": 0,
        "modules": 0,
        "startup": 0,
    }
    counts: dict[str, int] = {}
    for path in APP_ROOT.rglob("*.py"):
        if path == APP_ROOT / "runtime" / "settings.py":
            continue
        if path.is_relative_to(APP_ROOT / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        imports_compat = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.runtime.settings"
            and any(alias.name.lower().endswith("compat") for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imports_compat:
            continue
        relative = path.relative_to(APP_ROOT)
        group = relative.parts[0] if len(relative.parts) > 1 else relative.as_posix()
        counts[group] = counts.get(group, 0) + 1

    unexpected = set(counts) - set(limits)
    exceeded = {group: count for group, count in counts.items() if group in limits and count > limits[group]}
    assert unexpected == set()
    assert exceeded == {}


def test_global_settings_imports_stay_within_compatibility_baseline():
    """真实 Settings 对象只能保留在已知迁移点和插件 SDK，不得产生新宿主调用。"""
    allowed = {
        "app/sdk/config.py",
        "app/startup/initializers/modules.py",
    }
    imports: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        if path.is_relative_to(APP_ROOT / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.runtime.config"
            and any(alias.name == "settings" for alias in node.names)
            for node in ast.walk(tree)
        ):
            imports.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert imports <= allowed


def test_api_does_not_import_factory():
    """装配器（factory）只允许 app.main 使用，HTTP 端点不得回引。"""
    violations: dict[str, set[str]] = {}
    for module_name, dependencies in _build_module_graph().items():
        if not module_name.startswith("app.api"):
            continue
        forbidden = {dependency for dependency in dependencies if dependency.startswith("app.factory")}
        if forbidden:
            violations[module_name] = forbidden
    assert violations == {}


def test_web_agent_non_transport_state_stays_in_application():
    """Agent 端点不得重新拥有附件、音频、投影或会话写入实现。"""
    endpoint_path = APP_ROOT / "api" / "endpoints" / "agent.py"
    application_path = APP_ROOT / "application" / "messaging" / "agent.py"
    endpoint_tree = ast.parse(
        endpoint_path.read_text(encoding="utf-8-sig"),
        filename=str(endpoint_path),
    )
    application_tree = ast.parse(
        application_path.read_text(encoding="utf-8-sig"),
        filename=str(application_path),
    )
    endpoint_owners = {
        node.name
        for node in endpoint_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    application_owners = {
        node.name
        for node in application_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "WebAgentEventPublisher",
        "apply_web_agent_display_event",
        "build_web_agent_command_items",
        "build_web_agent_input_attachments",
        "build_web_agent_message_events_async",
        "build_web_agent_session_id_async",
        "build_web_agent_stream",
        "collect_web_agent_traditional_events",
        "get_web_agent_registered_file",
        "prepare_web_agent_audio_attachment_path_async",
        "save_web_agent_display_snapshot",
        "transcribe_web_agent_audio_input",
    }
    forbidden_endpoint_imports = {
        "app.application.commands",
        "app.application.messaging.router",
        "app.chain.message",
        "app.runtime.execution",
        "app.runtime.stop",
    }
    endpoint_imports = {
        node.module for node in ast.walk(endpoint_tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert required <= application_owners
    assert required.isdisjoint(endpoint_owners)
    assert endpoint_imports.isdisjoint(forbidden_endpoint_imports)


def test_plugin_market_use_cases_stay_in_application():
    """Plugin 端点不得重新组合市场 Adapter 或拥有目录投影实现。"""
    endpoint_path = APP_ROOT / "api" / "endpoints" / "plugin.py"
    endpoint_tree = ast.parse(
        endpoint_path.read_text(encoding="utf-8-sig"),
        filename=str(endpoint_path),
    )
    forbidden_imports = {
        "app.adapters.external.market",
        "app.adapters.external.server",
    }
    imported = {node.module for node in ast.walk(endpoint_tree) if isinstance(node, ast.ImportFrom) and node.module}
    endpoint_owners = {
        node.name
        for node in endpoint_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert forbidden_imports.isdisjoint(imported)
    assert {
        "_get_market_plugin_from_repo",
        "_get_plugin_history_detail",
        "_installed_plugins_with_declared_metadata",
        "_prepare_update_candidates",
    }.isdisjoint(endpoint_owners)


def test_system_nettest_is_owned_by_application_service():
    """System API 不得重新拥有网络目标目录、安全准入或具体传输组装。"""
    endpoint_path = APP_ROOT / "api" / "endpoints" / "system.py"
    endpoint_tree = ast.parse(
        endpoint_path.read_text(encoding="utf-8-sig"),
        filename=str(endpoint_path),
    )
    retired_endpoint_symbols = {
        "_build_nettest_rules",
        "_close_nettest_response",
        "_get_nettest_rule",
        "_is_allowed_nettest_redirect",
        "_match_nettest_prefix",
        "_validate_nettest_url",
    }
    endpoint_functions = {
        node.name for node in endpoint_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert endpoint_functions.isdisjoint(retired_endpoint_symbols)
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "NetworkTestService"
        for node in ast.walk(endpoint_tree)
    )

    application_path = APP_ROOT / "application" / "network.py"
    application_tree = ast.parse(
        application_path.read_text(encoding="utf-8-sig"),
        filename=str(application_path),
    )
    concrete_adapter_imports = {
        node.module
        for node in ast.walk(application_tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.adapters")
    }
    concrete_adapter_imports.update(
        alias.name
        for node in ast.walk(application_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("app.adapters")
    )
    assert concrete_adapter_imports == set()


def test_plugin_market_construction_is_owned_by_startup_composition():
    """插件 initializer 只能消费组合根创建的市场依赖，禁止隐式构造第二套 owner。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "plugins.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    initializer_imports = {
        alias.name for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "compose_plugin_market" in initializer_imports
    forbidden_names = {
        "PluginMarketTransport",
        "PluginPackageSourceClient",
        "PluginPackageManager",
        "PluginRuntimeHealth",
        "PluginDependencyInstaller",
    }
    assert forbidden_names.isdisjoint(initializer_imports)
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_names
        for node in ast.walk(initializer_tree)
    )

    composition_path = APP_ROOT / "startup" / "composition" / "plugin.py"
    composition_tree = ast.parse(
        composition_path.read_text(encoding="utf-8-sig"),
        filename=str(composition_path),
    )
    composition_calls = [
        node.func.id
        for node in ast.walk(composition_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_names
    ]
    assert composition_calls.count("PluginMarketTransport") == 1
    assert composition_calls.count("PluginPackageSourceClient") == 1
    assert composition_calls.count("PluginPackageManager") == 1
    assert composition_calls.count("PluginRuntimeHealth") == 1
    assert composition_calls.count("PluginDependencyInstaller") == 1


def test_plugin_market_transport_is_process_shared_across_compat_and_composition():
    """兼容门面和组合根不得物化两套插件市场传输 owner。"""
    from app.adapters.external.market import _plugin_market_transport
    from app.adapters.external.plugin.client import PluginMarketTransport

    assert PluginMarketTransport.get_existing_instance() is _plugin_market_transport


def test_plugin_runtime_owners_are_shared_with_compat_facade():
    """兼容门面必须消费组合根发布的健康检查与依赖安装 owner。"""
    from app.adapters.external.market import (
        _plugin_dependency_owner,
        _plugin_runtime_health_owner,
        configure_plugin_runtime_owners,
        reset_plugin_runtime_owners,
    )

    health = object()
    dependency = object()
    configure_plugin_runtime_owners(
        health=health,  # type: ignore[arg-type]
        dependency=dependency,  # type: ignore[arg-type]
    )
    try:
        assert _plugin_runtime_health_owner() is health
        assert _plugin_dependency_owner() is dependency
    finally:
        reset_plugin_runtime_owners()


def test_domain_initializer_delegates_construction_to_composition():
    """领域 initializer 只负责生命周期调用，不得直接构造 Adapter 或领域服务。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "domain.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {"app.startup.composition.domain"}
    calls = [ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)]
    assert calls == ["compose_domain_dependencies"]

    composition_path = APP_ROOT / "startup" / "composition" / "domain.py"
    composition_tree = ast.parse(
        composition_path.read_text(encoding="utf-8-sig"),
        filename=str(composition_path),
    )
    composition_imports = {
        node.module for node in ast.walk(composition_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.adapters.network.resolver" in composition_imports
    assert "app.adapters.system.host" in composition_imports
    assert "app.application.recognition" in composition_imports


def test_network_initializer_delegates_construction_to_composition():
    """网络 initializer 只保留生命周期入口，不得重新拥有具体技术适配器。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "network.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {"app.startup.composition.network"}
    calls = {ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)}
    assert calls == {
        "configure_chain_network_composition",
        "reset_chain_network_composition",
    }

    composition_path = APP_ROOT / "startup" / "composition" / "network.py"
    composition_tree = ast.parse(
        composition_path.read_text(encoding="utf-8-sig"),
        filename=str(composition_path),
    )
    composition_imports = {
        node.module for node in ast.walk(composition_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.adapters.network.http" in composition_imports
    assert "app.adapters.system.host" in composition_imports
    assert "app.chain.download.ports" in composition_imports


def test_site_initializer_delegates_construction_to_composition():
    """站点 initializer 只保留生命周期入口，不得重新拥有具体技术适配器。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "site.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {"app.startup.composition.site"}
    calls = {ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)}
    assert calls == {
        "configure_site_access_composition",
        "reset_site_access_composition",
    }

    composition_path = APP_ROOT / "startup" / "composition" / "site.py"
    composition_tree = ast.parse(
        composition_path.read_text(encoding="utf-8-sig"),
        filename=str(composition_path),
    )
    composition_imports = {
        node.module for node in ast.walk(composition_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.adapters.external.cookiecloud" in composition_imports
    assert "app.adapters.network.browser" in composition_imports
    assert "app.application.rss" in composition_imports


def test_chain_initializer_delegates_construction_to_composition():
    """Chain initializer 只保留生命周期入口，不得重新拥有外部技术适配器。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "chain.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {"app.startup.composition.chain"}
    calls = {ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)}
    assert calls == {
        "configure_chain_port_composition",
        "reset_chain_port_composition",
    }

    composition_path = APP_ROOT / "startup" / "composition" / "chain.py"
    composition_tree = ast.parse(
        composition_path.read_text(encoding="utf-8-sig"),
        filename=str(composition_path),
    )
    composition_imports = {
        node.module for node in ast.walk(composition_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.adapters.external.server" in composition_imports
    assert "app.adapters.system.host" in composition_imports
    assert "app.chain._recognition" in composition_imports


def test_cache_initializer_delegates_construction_to_composition():
    """缓存 initializer 只保留启动入口，不得直接登记具体缓存 Adapter。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "cache.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {"app.startup.composition.cache"}
    calls = [ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)]
    assert calls == ["configure_cache_composition"]


def test_resource_initializer_delegates_construction_to_composition():
    """资源 initializer 只保留生命周期入口，不得构造托管资源 Adapter。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "resources.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported_modules == {
        "app.runtime.capabilities.runtime",
        "app.startup.composition.resource",
    }
    constructors = {ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)}
    assert constructors == {
        "configure_managed_resource_composition",
        "stop_managed_resource_composition",
        "reset_managed_resource_composition",
    }


def test_modules_initializer_consumes_doh_and_workflow_composition():
    """模块 initializer 不得直接构造 DoH 或事务型工作流执行 Adapter。"""
    initializer_path = APP_ROOT / "startup" / "initializers" / "modules.py"
    initializer_tree = ast.parse(
        initializer_path.read_text(encoding="utf-8-sig"),
        filename=str(initializer_path),
    )
    imported_modules = {
        node.module for node in ast.walk(initializer_tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.adapters.network.doh" not in imported_modules
    assert "app.db.adapters.workflow" not in imported_modules
    calls = {ast.unparse(node.func) for node in ast.walk(initializer_tree) if isinstance(node, ast.Call)}
    assert "DohHelper" not in calls
    assert "TransactionalWorkflowExecutionService" not in calls
    assert "configure_doh_composition" in calls
    assert "configure_workflow_execution_composition" in calls


def test_system_api_business_orchestration_is_owned_by_application_service():
    """System API 只保留传输映射，不得重新拥有日志、设置或更新实现。"""
    endpoint_path = APP_ROOT / "api" / "endpoints" / "system.py"
    endpoint_tree = ast.parse(
        endpoint_path.read_text(encoding="utf-8-sig"),
        filename=str(endpoint_path),
    )
    imported = {node.module for node in ast.walk(endpoint_tree) if isinstance(node, ast.ImportFrom) and node.module}
    direct_adapters = {
        module
        for module in imported
        if module.startswith("app.adapters") and module != "app.adapters.web.security.access"
    }
    endpoint_functions = {
        node.name for node in endpoint_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    retired_helpers = {
        "_collect_named_log_files",
        "_validate_database_backup_config",
        "_validate_llm_server_tool_config",
        "_is_allowed_plugin_market_wiki_url",
    }

    assert not direct_adapters
    assert endpoint_functions.isdisjoint(retired_helpers)
    assert not imported.intersection(
        {
            "app.application.plugin.runtime",
            "app.runtime.events",
            "app.runtime.state",
        }
    )

    application_path = APP_ROOT / "application" / "system.py"
    application_tree = ast.parse(
        application_path.read_text(encoding="utf-8-sig"),
        filename=str(application_path),
    )
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.adapters")
        for node in ast.walk(application_tree)
    )

    composition_source = (APP_ROOT / "startup" / "composition" / "system.py").read_text(encoding="utf-8-sig")
    assert "def compose_system_service(" in composition_source
    assert "system=compose_system_service(" in (APP_ROOT / "startup" / "composition" / "runtime.py").read_text(
        encoding="utf-8-sig"
    )


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
        roots = {name.split(".")[1] for name in component if name.startswith("app.") and name.count(".") >= 1}
        involved = {root for root in roots if f"app.{root}" in PROCESS_LEVEL_ROOTS}
        if len(involved) > 1:
            violations.append(sorted(component))
    assert violations == []
