"""Agent 消息应用层职责拆分门禁。"""

import ast
import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MESSAGING_ROOT = PROJECT_ROOT / "app" / "application" / "messaging"
AGENT_FACADE_PATH = MESSAGING_ROOT / "agent.py"
AGENT_INTERACTION_PATH = MESSAGING_ROOT / "agent_interaction.py"
CHANNEL_ADMIN_PATH = MESSAGING_ROOT / "channel_admin.py"


def _top_level_owners(path: Path) -> set[str]:
    """返回模块顶层定义的类和函数名称。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_agent_interaction_has_one_canonical_owner() -> None:
    """Agent 选择交互只能在独立模块实现，旧入口保留对象级兼容导出。"""
    names = {
        "AgentInteractionManager",
        "AgentInteractionOption",
        "PendingAgentInteraction",
        "build_agent_choice_button_rows",
        "build_agent_choice_callback",
        "parse_agent_choice_callback",
    }
    facade = importlib.import_module("app.application.messaging.agent")
    interaction = importlib.import_module(
        "app.application.messaging.agent_interaction"
    )

    assert names <= _top_level_owners(AGENT_INTERACTION_PATH)
    assert names.isdisjoint(_top_level_owners(AGENT_FACADE_PATH))
    for name in names | {"agent_interaction_manager"}:
        assert getattr(facade, name) is getattr(interaction, name)


def test_channel_admin_has_one_canonical_owner() -> None:
    """渠道管理员匹配只能在权限模块实现，旧 Agent 入口保留兼容导出。"""
    names = {
        "matches_channel_admin",
        "register_channel_admin_resolver",
        "resolve_config_principal_ids",
    }
    facade = importlib.import_module("app.application.messaging.agent")
    channel_admin = importlib.import_module(
        "app.application.messaging.channel_admin"
    )

    assert names <= _top_level_owners(CHANNEL_ADMIN_PATH)
    assert names.isdisjoint(_top_level_owners(AGENT_FACADE_PATH))
    for name in names:
        assert getattr(facade, name) is getattr(channel_admin, name)
