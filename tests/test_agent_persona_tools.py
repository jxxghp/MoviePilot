"""Agent 统一人格工具测试。"""

import json
from pathlib import Path

import pytest

from app.agent.runtime import AgentRuntimeManager
from app.agent.tools.impl.persona import PersonaTool


@pytest.fixture
def anyio_backend() -> str:
    """限定异步用例使用 asyncio 后端。"""
    return "asyncio"


@pytest.fixture
def runtime_manager(tmp_path: Path) -> AgentRuntimeManager:
    """构造与真实 Agent 配置隔离的人格运行时。"""
    defaults_root = Path(__file__).resolve().parents[1] / "app" / "agent" / "defaults"
    manager = AgentRuntimeManager(
        agent_root_dir=tmp_path / "agent",
        bundled_defaults_dir=defaults_root,
    )
    manager.ensure_layout()
    return manager


def _tool(*, is_admin: bool = False) -> PersonaTool:
    """构造带可信管理员事实的人格工具。"""
    tool = PersonaTool(session_id="session-1", user_id="10001")
    tool.set_agent_context({"is_admin": is_admin})
    return tool


@pytest.mark.anyio
async def test_persona_list_returns_available_and_active_state(
    monkeypatch,
    runtime_manager: AgentRuntimeManager,
) -> None:
    """list 动作应返回可用人格和当前激活状态。"""
    monkeypatch.setattr(
        "app.agent.tools.impl.persona.agent_runtime_manager",
        runtime_manager,
    )

    payload = json.loads(await _tool().run(action="list"))

    assert payload["active_persona"] == "default"
    assert payload["count"] >= 9
    assert any(item["persona_id"] == "concise" for item in payload["personas"])
    assert any(item["persona_id"] == "catgirl" for item in payload["personas"])
    assert any(item["is_active"] for item in payload["personas"])


@pytest.mark.anyio
async def test_persona_switch_updates_runtime_by_alias(
    monkeypatch,
    runtime_manager: AgentRuntimeManager,
) -> None:
    """switch 动作应接受别名并持久化激活人格。"""
    monkeypatch.setattr(
        "app.agent.tools.impl.persona.agent_runtime_manager",
        runtime_manager,
    )

    payload = json.loads(await _tool().run(action="switch", persona_id="讲解"))

    assert payload["success"] is True
    assert payload["active_persona"] == "guide"
    assert runtime_manager.load_runtime_config().active_persona == "guide"


@pytest.mark.anyio
async def test_persona_update_changes_existing_definition(
    monkeypatch,
    runtime_manager: AgentRuntimeManager,
) -> None:
    """管理员 update 动作应更新既有人格定义。"""
    monkeypatch.setattr(
        "app.agent.tools.impl.persona.agent_runtime_manager",
        runtime_manager,
    )

    payload = json.loads(
        await _tool(is_admin=True).run(
            action="update",
            persona_id="default",
            description="更偏执行导向的默认人格。",
            append_instructions=["Prefer action-first responses."],
        )
    )

    assert payload["success"] is True
    assert payload["created"] is False
    default_persona = next(
        item for item in runtime_manager.load_runtime_config().available_personas if item.persona_id == "default"
    )
    assert default_persona.description == "更偏执行导向的默认人格。"
    assert "Prefer action-first responses." in default_persona.text


@pytest.mark.anyio
async def test_persona_update_can_create_new_definition(
    monkeypatch,
    runtime_manager: AgentRuntimeManager,
) -> None:
    """管理员 update 动作应能显式创建新人格。"""
    monkeypatch.setattr(
        "app.agent.tools.impl.persona.agent_runtime_manager",
        runtime_manager,
    )

    payload = json.loads(
        await _tool(is_admin=True).run(
            action="update",
            persona_id="analysis",
            label="分析型",
            description="更适合解释复杂问题。",
            aliases=["分析", "推理"],
            instructions=("- Tone: analytical and structured.\n- For complex tasks, explain the key tradeoff briefly."),
            create_if_missing=True,
        )
    )

    assert payload["success"] is True
    assert payload["created"] is True
    created = next(
        item for item in runtime_manager.load_runtime_config().available_personas if item.persona_id == "analysis"
    )
    assert created.label == "分析型"
    assert "推理" in created.aliases
    assert "analytical and structured" in created.text


@pytest.mark.anyio
async def test_persona_update_requires_admin(
    monkeypatch,
    runtime_manager: AgentRuntimeManager,
) -> None:
    """普通用户可以查询和切换，但不能更新人格定义。"""
    monkeypatch.setattr(
        "app.agent.tools.impl.persona.agent_runtime_manager",
        runtime_manager,
    )

    payload = json.loads(
        await _tool().run(
            action="update",
            persona_id="default",
            description="不应写入",
        )
    )

    assert payload["success"] is False
    assert "系统管理员" in payload["message"]
