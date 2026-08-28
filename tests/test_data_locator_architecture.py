"""Chain 与 Agent 数据依赖显式注入的架构门禁。"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"
RETIRED_MODULES = {
    "app.application.agentdata",
    "app.application.chain.data",
}
RETIRED_GETTERS = {
    "_get_subscribe_writer",
    "configure_delete_subscribe_scope",
    "configure_subscribe_writer",
    "configure_subscription_completion_scope",
    "configure_subscription_mutation_scope",
    "configure_sync_delete_subscribe_scope",
    "get_agent_chat_port",
    "get_agent_download_history_port",
    "get_agent_plugin_data_port",
    "get_agent_site_port",
    "get_agent_subscribe_history_port",
    "get_agent_subscribe_port",
    "get_agent_task_port",
    "get_agent_transfer_history_port",
    "get_agent_user_port",
    "get_chain_download_failure_port",
    "get_chain_download_history_port",
    "get_chain_media_server_port",
    "get_chain_site_port",
    "get_chain_subscribe_port",
    "get_chain_transfer_execution_port",
    "get_chain_transfer_history_port",
    "get_chain_transfer_pending_port",
    "get_chain_user_port",
    "get_delete_subscribe_scope",
    "get_subscription_completion_scope",
    "get_subscription_mutation_scope",
    "get_sync_delete_subscribe_scope",
}


def _canonical_paths() -> list[Path]:
    """返回排除插件副本与兼容实现后的宿主 Python 文件。"""
    return [
        path
        for path in sorted(APP_ROOT.rglob("*.py"))
        if "plugins" not in path.relative_to(APP_ROOT).parts
        and path.relative_to(APP_ROOT).parts[:2] != ("runtime", "compat")
        and path.relative_to(APP_ROOT).parts[:2] != ("sdk", "_legacy")
    ]


def _class_fields(path: Path, class_name: str) -> dict[str, str]:
    """读取指定数据上下文的源码级字段注解。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in class_node.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }


def test_retired_data_locator_modules_and_getters_cannot_return() -> None:
    """Canonical 宿主不得复活全局数据注册表、导入或动态 getter 调用。"""
    assert not (APP_ROOT / "application" / "agentdata.py").exists()
    assert not (APP_ROOT / "application" / "chain" / "data.py").exists()

    violations: list[str] = []
    for path in _canonical_paths():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in RETIRED_MODULES:
                violations.append(f"{relative}:{node.lineno}:import:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in RETIRED_MODULES:
                        violations.append(f"{relative}:{node.lineno}:import:{alias.name}")
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in RETIRED_GETTERS
            ):
                violations.append(f"{relative}:{node.lineno}:definition:{node.name}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in RETIRED_GETTERS
            ):
                violations.append(f"{relative}:{node.lineno}:call:{node.func.id}")

    assert violations == []


def test_internal_data_locators_do_not_gain_sdk_or_compat_abi() -> None:
    """被删除的宿主注册表没有插件合同，不得转存到 SDK 或 Compat。"""
    compatibility_source = (
        APP_ROOT / "runtime" / "compat" / "manifest.py"
    ).read_text(encoding="utf-8-sig")
    sdk_sources = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in sorted((APP_ROOT / "sdk").rglob("*.py"))
    )

    assert all(module not in compatibility_source for module in RETIRED_MODULES)
    assert all(name not in sdk_sources for name in RETIRED_GETTERS)


def test_injected_data_contexts_use_owned_typed_ports() -> None:
    """Chain 与 Agent 的持久化字段必须由明确 Port 注解拥有，不得用 Any 遮蔽。"""
    chain_fields = _class_fields(
        APP_ROOT / "application" / "chain" / "context.py",
        "ChainRuntimeContext",
    )
    agent_fields = _class_fields(
        APP_ROOT / "application" / "agent.py",
        "AgentDataContext",
    )
    expected_chain_fields = {
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
    }
    expected_agent_fields = {
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
    }

    assert expected_chain_fields <= chain_fields.keys()
    assert expected_agent_fields <= agent_fields.keys()
    assert all("Any" not in chain_fields[name] for name in expected_chain_fields)
    assert all("Any" not in agent_fields[name] for name in expected_agent_fields)
