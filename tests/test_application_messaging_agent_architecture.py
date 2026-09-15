"""Agent 消息应用层职责拆分门禁。"""

import ast
import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MESSAGING_ROOT = PROJECT_ROOT / "app" / "application" / "messaging"
AGENT_FACADE_PATH = MESSAGING_ROOT / "agent.py"
AGENT_INTERACTION_PATH = MESSAGING_ROOT / "agent_interaction.py"


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
