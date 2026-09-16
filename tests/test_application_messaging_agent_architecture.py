"""Agent 消息应用层职责拆分门禁。"""

import ast
import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MESSAGING_ROOT = PROJECT_ROOT / "app" / "application" / "messaging"
AGENT_FACADE_PATH = MESSAGING_ROOT / "agent.py"
AGENT_INTERACTION_PATH = MESSAGING_ROOT / "interaction" / "agent.py"
CHANNEL_ADMIN_PATH = MESSAGING_ROOT / "channel" / "admin.py"
WEB_AGENT_ROOT = MESSAGING_ROOT / "webagent"
WEB_AGENT_EVENTS_PATH = WEB_AGENT_ROOT / "events.py"
WEB_AGENT_STREAM_PATH = WEB_AGENT_ROOT / "stream.py"


def _top_level_owners(path: Path) -> set[str]:
    """返回模块顶层定义的类和函数名称。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_agent_interaction_has_one_canonical_owner() -> None:
    """Agent 选择交互只能在独立模块实现，根模块不再暴露重复入口。"""
    names = {
        "AgentInteractionManager",
        "AgentInteractionOption",
        "PendingAgentInteraction",
        "build_agent_choice_button_rows",
        "build_agent_choice_callback",
        "parse_agent_choice_callback",
    }
    facade = importlib.import_module("app.application.messaging.agent")
    importlib.import_module("app.application.messaging.interaction.agent")

    assert names <= _top_level_owners(AGENT_INTERACTION_PATH)
    assert names.isdisjoint(_top_level_owners(AGENT_FACADE_PATH))
    assert all(not hasattr(facade, name) for name in names | {"agent_interaction_manager"})


def test_channel_admin_has_one_canonical_owner() -> None:
    """渠道管理员匹配只能在权限模块实现，根 Agent 模块不再暴露重复入口。"""
    names = {
        "matches_channel_admin",
        "register_channel_admin_resolver",
        "resolve_config_principal_ids",
    }
    facade = importlib.import_module("app.application.messaging.agent")
    importlib.import_module("app.application.messaging.channel.admin")

    assert names <= _top_level_owners(CHANNEL_ADMIN_PATH)
    assert names.isdisjoint(_top_level_owners(AGENT_FACADE_PATH))
    assert all(not hasattr(facade, name) for name in names)


def test_web_agent_event_bridge_has_one_canonical_owner() -> None:
    """WebAgent 通知与编辑桥接只能在独立模块实现，根 Agent 模块不再暴露重复入口。"""
    names = {
        "attach_web_agent_edit_queue",
        "attach_web_agent_message_queue",
        "build_web_agent_message_update_event",
        "detach_web_agent_edit_queue",
        "detach_web_agent_message_queue",
        "dispatch_web_agent_edit_event",
        "dispatch_web_agent_message_event",
        "edit_web_agent_message",
        "extract_web_agent_message_from_event_data",
        "is_web_agent_message_for_user",
        "normalize_web_agent_button_rows",
    }
    facade = importlib.import_module("app.application.messaging.agent")
    importlib.import_module("app.application.messaging.webagent.events")

    assert names <= _top_level_owners(WEB_AGENT_EVENTS_PATH)
    assert names.isdisjoint(_top_level_owners(AGENT_FACADE_PATH))
    assert all(not hasattr(facade, name) for name in names)


def test_web_agent_modules_share_one_subpackage() -> None:
    """同前缀 WebAgent 实现集中在子包，根目录不得保留平级门面。"""
    names = {
        "WebAgentStreamDependencies",
        "build_agent_web_agent_stream",
        "submit_web_agent_steering",
    }
    canonical = importlib.import_module(
        "app.application.messaging.webagent.stream"
    )

    assert names <= _top_level_owners(WEB_AGENT_STREAM_PATH)
    assert {path.name for path in WEB_AGENT_ROOT.glob("*.py")} == {
        "__init__.py",
        "events.py",
        "stream.py",
    }
    assert not list(MESSAGING_ROOT.glob("webagent*.py"))
    assert all(hasattr(canonical, name) for name in names)
