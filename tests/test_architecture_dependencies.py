import ast
import json
from functools import lru_cache
from pathlib import Path

from scripts.architecture.baseline import (
    collect_event_contracts as _collect_event_contracts,
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
DEPENDENCY_POLICY_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "dependency-policy.json"
)
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
    "app/adapters/network/rss.py",
    "app/adapters/network/sites.pyi",
    "app/application/plugins.py",
    "app/application/subscribe.py",
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
        "app.runtime.extensions.plugin_manager",
        "app.runtime.extensions.module_manager",
        "app.scheduler",
    ),
    "app.agent": (
        "app.runtime.extensions.plugin_manager",
        "app.runtime.extensions.module_manager",
    ),
    "app.chain": (
        "app.runtime.extensions.plugin_manager",
        "app.runtime.extensions.module_manager",
        "app.runtime.extensions.module.dispatcher",
    ),
    "app.workflow": (
        "app.runtime.extensions.plugin_manager",
        "app.runtime.extensions.module_manager",
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


def test_startup_root_contains_only_composition_packages():
    """组合根顶层只保留稳定分区，禁止再次堆叠扁平实现文件。"""
    startup_root = APP_ROOT / "startup"
    root_modules = sorted(path.name for path in startup_root.glob("*.py"))
    python_packages = sorted(
        path.name
        for path in startup_root.iterdir()
        if path.is_dir() and any(path.rglob("*.py"))
    )

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
                target_names = {
                    target.id for target in node.targets if isinstance(target, ast.Name)
                }
                if (
                    "SystemConfigOper" in target_names
                    and node.value.id == "get_configured_system_config"
                ):
                    violations.append(
                        f"{relative.as_posix()}:{node.lineno}:SystemConfigOper"
                    )
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
                if (
                    alias.name in forbidden_names
                    or class_shaped_plugin_getter
                    or class_shaped_config_getter
                ):
                    imported_name = alias.asname or alias.name
                    violations.append(
                        f"{relative.as_posix()}:{node.lineno}:{imported_name}"
                    )

    assert violations == []


def test_workflow_domain_uses_explicit_chain_data_port_getters():
    """工作流域不得重新使用迁移期 PortProxy 冒充数据库 Oper。"""
    paths = [APP_ROOT / "chain" / "workflow.py"]
    paths.extend((APP_ROOT / "workflow").rglob("*.py"))
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.application.chain.data":
                continue
            for alias in node.names:
                if alias.name.endswith("PortProxy"):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:{alias.name}"
                    )

    assert violations == []


def test_user_and_messaging_chains_use_explicit_data_port_getters():
    """用户、交互和消息链不得把迁移期 UserPortProxy 伪装成 UserOper。"""
    paths = [
        APP_ROOT / "chain" / "user.py",
        APP_ROOT / "chain" / "interaction.py",
        APP_ROOT / "chain" / "_messaging.py",
    ]
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.application.chain.data":
                continue
            for alias in node.names:
                if alias.name == "UserPortProxy":
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
                    )

    assert violations == []


def test_music_chain_uses_explicit_subscribe_data_port_getter():
    """音乐订阅链不得把 SubscribePortProxy 伪装成 SubscribeOper。"""
    path = APP_ROOT / "chain" / "_music.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.chain.data"
        and any(alias.name == "SubscribePortProxy" for alias in node.names)
    ]

    assert violations == []


def test_site_chains_use_explicit_site_data_port_getter():
    """站点与种子链不得把 SitePortProxy 伪装成 SiteOper。"""
    paths = [APP_ROOT / "chain" / "site.py", APP_ROOT / "chain" / "torrents.py"]
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.application.chain.data":
                continue
            if any(alias.name == "SitePortProxy" for alias in node.names):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
                )

    assert violations == []


def test_mediaserver_chain_uses_explicit_data_port_getter():
    """媒体服务器链不得把 MediaServerPortProxy 伪装成 MediaServerOper。"""
    path = APP_ROOT / "chain" / "mediaserver.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.chain.data"
        and any(alias.name == "MediaServerPortProxy" for alias in node.names)
    ]

    assert violations == []


def test_download_chain_uses_explicit_data_port_getters():
    """下载链不得把三个迁移期 PortProxy 伪装成数据库 Oper。"""
    path = APP_ROOT / "chain" / "download.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    forbidden = {
        "DownloadFailurePortProxy",
        "DownloadHistoryPortProxy",
        "MediaServerPortProxy",
    }
    violations = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.chain.data"
        for alias in node.names
        if alias.name in forbidden
    ]

    assert violations == []


def test_subscribe_chain_uses_explicit_data_port_getters():
    """订阅链不得把三个迁移期 PortProxy 伪装成数据库 Oper。"""
    path = APP_ROOT / "chain" / "subscribe.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    forbidden = {
        "DownloadHistoryPortProxy",
        "SitePortProxy",
        "SubscribePortProxy",
    }
    violations = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.chain.data"
        for alias in node.names
        if alias.name in forbidden
    ]

    assert violations == []


def test_transfer_chains_use_explicit_data_port_getters():
    """整理主链与 mixin 不得把迁移期 PortProxy 伪装成数据库 Oper。"""
    paths = [APP_ROOT / "chain" / "transfer.py", APP_ROOT / "chain" / "_transfer.py"]
    forbidden = {
        "DownloadHistoryPortProxy",
        "TransferHistoryPortProxy",
        "TransferPendingPortProxy",
    }
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.application.chain.data":
                continue
            for alias in node.names:
                if alias.name in forbidden:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:{alias.name}"
                    )

    assert violations == []


def test_agent_consumers_use_explicit_data_port_getters():
    """Agent 生产模块不得把兼容数据端口代理重新伪装成数据库 Oper。"""
    forbidden = {
        "AgentChatPort",
        "AgentTaskPort",
        "DownloadHistoryPort",
        "PluginDataPort",
        "SitePort",
        "SubscribeHistoryPort",
        "SubscribePort",
        "TransferHistoryPort",
        "UserPort",
        "WorkflowPort",
    }
    violations: list[str] = []
    for path in (APP_ROOT / "agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.application.agentdata":
                continue
            for alias in node.names:
                if alias.name in forbidden:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:{alias.name}"
                    )

    assert violations == []


def test_scheduler_does_not_depend_on_database_implementation():
    """Scheduler 只能消费应用端口，不得重新直连 app.db 实现。"""
    dependencies = _build_module_graph().get("app.scheduler", set())
    assert {
        dependency
        for dependency in dependencies
        if dependency == "app.db" or dependency.startswith("app.db.")
    } == set()


def test_agent_task_async_execution_uses_application_service():
    """AgentTask async 执行不得经动态数据端口隐藏同步 Oper 调用。"""
    path = APP_ROOT / "agent" / "orchestrator.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.application.agentdata"
        and any(alias.name == "get_agent_task_port" for alias in node.names)
    ]
    assert violations == []


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
        APP_ROOT / "scheduler.py",
        APP_ROOT / "agent" / "llm" / "capability.py",
        APP_ROOT / "agent" / "tools" / "base.py",
        APP_ROOT / "agent" / "tools" / "impl" / "query_library_latest.py",
        APP_ROOT / "api" / "endpoints" / "mediaserver.py",
    ]
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "app.runtime.extensions.service_config"
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}"
                )
            if (
                path.name == "mediaserver.py"
                and isinstance(node, ast.ImportFrom)
                and node.module == "app.application.mediaserver"
                and any(alias.name == "MediaServerHelper" for alias in node.names)
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}:MediaServerHelper"
                )

    assert violations == []


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
            base_class = next(
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "Base"
            )
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
            defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(
                node.args.defaults
            )
            for argument, default in zip(arguments, defaults):
                if argument.arg != "db":
                    continue
                annotation = ast.unparse(argument.annotation) if argument.annotation else ""
                if default is not None or "None" in annotation:
                    violations.append(
                        f"{relative}:{node.lineno}:{node.name}:optional-db"
                    )
    assert violations == []


def test_plugin_sdk_does_not_import_or_export_host_models():
    """插件 SDK 只能暴露 Oper，不得把宿主 ORM Model 作为插件接口。"""
    violations: list[str] = []
    for path in (APP_ROOT / "sdk").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "app.db.models"
                or node.module.startswith("app.db.models.")
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{node.module}"
                )
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
        source: sorted(
            dependency
            for dependency in dependencies
            if dependency.startswith("app.db")
        )
        for source, dependencies in graph.items()
        if source.startswith(layer_roots)
        and any(dependency.startswith("app.db") for dependency in dependencies)
    }
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


def _load_dependency_policy() -> dict:
    """读取人工审查的依赖语义 policy；生成基线不得改写该文件。"""
    return json.loads(DEPENDENCY_POLICY_PATH.read_text(encoding="utf-8"))


def _scc_policy_violations(
    graph: dict[str, set[str]],
    entries: list[dict],
) -> tuple[list[list[str]], list[list[str]]]:
    """返回未审查 SCC 与已经失效但未清理的 policy SCC。"""
    actual = {
        tuple(component)
        for component in _strongly_connected_components(graph)
    }
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
    assert {entry["classification"] for entry in entries} == {
        "contained_vendor",
        "temporary_debt",
    }
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(entry["modules"] == sorted(set(entry["modules"])) for entry in entries)
    assert all(entry["reason"] and entry["tracking"] for entry in entries)
    all_members = [member for entry in entries for member in entry["modules"]]
    assert len(all_members) == len(set(all_members))

    temporary = next(
        entry for entry in entries if entry["classification"] == "temporary_debt"
    )
    assert temporary["id"] == "chain-package-root"
    assert temporary["tracking"] == "ARCH-107"
    assert temporary["modules"] == [
        "app.chain",
        "app.chain._messaging",
        "app.chain._recognition",
    ]

    vendor = next(
        entry for entry in entries if entry["classification"] == "contained_vendor"
    )
    assert vendor["id"] == "themoviedb-vendored-package"
    assert vendor["tracking"] == "replace-or-upgrade-vendored-package"
    assert len(vendor["modules"]) == 29
    assert all(
        member.startswith("app.modules.themoviedb")
        for member in vendor["modules"]
    )

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
    assert all(
        not module.startswith("app.plugins")
        for module in _discover_modules()
    )


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
            if isinstance(node, ast.ImportFrom)
            and node.module == "app.agent.runtime_loader"
            for alias in node.names
            if alias.name in forbidden
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
            if isinstance(node, ast.ImportFrom)
            and node.module in {"app.agent.llm", "app.agent.llm.provider"}
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
            if isinstance(node, ast.ImportFrom)
            and node.module in {"app.agent.llm", "app.agent.llm.capability"}
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
        if module_name.startswith("app.application")
        and "app.runtime.events" in dependencies
    }
    assert violations == {}


def test_http_endpoints_do_not_register_process_event_listeners():
    """HTTP 端点不得拥有进程级事件监听器，监听装配必须留在 startup。"""
    contracts = _collect_event_contracts()
    callers = {
        item["caller"]
        for event in contracts["events"].values()
        for item in event["consumers"]
    }
    callers.update(item["caller"] for item in contracts["dynamic_consumers"])

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
    exceeded = {
        group: count
        for group, count in counts.items()
        if group in limits and count > limits[group]
    }
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
