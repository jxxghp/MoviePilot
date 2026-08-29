"""AgentManager 门面、职责 owner 与兼容路径合同。"""

import json
import subprocess
import sys

import pytest

from app.agent.lifecycle import AgentLifecycleOwner
from app.agent.manager import AgentManager
from app.agent.session import AgentSessionOwner
from app.agent.tasks import AgentTaskOwner


def _run_isolated(script: str) -> dict:
    """在干净解释器中执行包根延迟加载探针。"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_agent_package_root_uses_precise_lazy_contracts() -> None:
    """包根不得加载完整编排器，也不得转发白名单外名称。"""
    result = _run_isolated(
        """
import json
import sys

import app.agent as facade

initial = {
    "manager": "app.agent.manager" in sys.modules,
    "orchestrator": "app.agent.orchestrator" in sys.modules,
}
try:
    facade.NotAnAgentContract
except AttributeError:
    rejected = True
else:
    rejected = False

from app.agent import AgentManager
from app.agent.manager import AgentManager as CanonicalManager

print(json.dumps({
    "initial": initial,
    "identity": AgentManager is CanonicalManager,
    "rejected": rejected,
}))
"""
    )

    assert result == {
        "initial": {"manager": False, "orchestrator": False},
        "identity": True,
        "rejected": True,
    }


def test_agent_manager_methods_have_single_owners() -> None:
    """稳定门面只继承 owner 方法，不复制会话、生命周期或任务实现。"""
    assert AgentManager.__dict__.keys() >= {"__init__"}
    assert "process_message" not in AgentManager.__dict__
    assert "initialize" not in AgentManager.__dict__
    assert "run_background_prompt" not in AgentManager.__dict__

    assert AgentManager.process_message is AgentSessionOwner.process_message
    assert AgentManager.clear_session is AgentSessionOwner.clear_session
    assert AgentManager.get_session_status is AgentSessionOwner.get_session_status
    assert AgentManager.initialize is AgentLifecycleOwner.initialize
    assert AgentManager.close is AgentLifecycleOwner.close
    assert AgentManager.run_background_prompt is AgentTaskOwner.run_background_prompt
    assert AgentManager.execute_scheduled_task is AgentTaskOwner.execute_scheduled_task
    assert AgentManager.heartbeat_check_jobs is AgentTaskOwner.heartbeat_check_jobs


def test_legacy_orchestrator_manager_path_resolves_to_canonical_facade() -> None:
    """历史编排器导入只经精确 Compat 指向 canonical 门面。"""
    from importlib import import_module

    LegacyAgentManager = getattr(import_module("app.agent.orchestrator"), "AgentManager")

    assert LegacyAgentManager is AgentManager


def test_agent_manager_facade_keeps_public_method_set() -> None:
    """重构后继续提供宿主和插件已使用的稳定公开方法。"""
    expected = {
        "clear_session",
        "close",
        "configure_data_context",
        "execute_scheduled_task",
        "get_session_status",
        "heartbeat_check_jobs",
        "initialize",
        "is_session_busy",
        "matches_secret_confirmation",
        "process_message",
        "run_background_prompt",
        "stop_current_task",
    }

    missing = {name for name in expected if not callable(getattr(AgentManager, name, None))}
    assert missing == set()


def test_agent_manager_constructor_rejects_unknown_arguments() -> None:
    """稳定构造门面不得静默接收未定义依赖。"""
    with pytest.raises(TypeError):
        AgentManager(unknown=True)
