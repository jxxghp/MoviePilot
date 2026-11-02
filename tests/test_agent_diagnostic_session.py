"""
只读诊断会话的工具与中间件限制测试。
"""

from types import SimpleNamespace

from app.agent.middleware.code import CodeCaptureMiddleware, CodeExecutionMiddleware
from app.agent.middleware.jobs import JobsMiddleware
from app.agent.middleware.learning import LearningCaptureMiddleware, LearningMiddleware
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.plan import PlanMiddleware
from app.agent.middleware.recall import RecallMiddleware
from app.agent.middleware.selection import ToolSelectorMiddleware
from app.agent.middleware.skills import SkillsMiddleware
from app.agent.middleware.subagents import MoviePilotSubAgentMiddleware, SubAgentTaskControlMiddleware
from app.agent.middleware.usage import UsageMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.session import _agent_reuse_mismatch, _MessageTask

WRITE_TOOL_NAMES = (
    "write_file",
    "edit_file",
    "execute_code",
    "restart",
    "install_plugin",
    "run_command",
    "send_message",
)


def test_diagnostic_tool_whitelist_contains_only_read_tools():
    """
    诊断白名单只登记读取类工具，任何写工具名称都不能出现在其中。
    """
    names = MoviePilotAgent.DIAGNOSTIC_TOOL_NAMES

    assert {"read_log", "read_file", "query_doctor_report", "read_tool_result", "update_plan"} <= names
    assert not set(WRITE_TOOL_NAMES) & names


def test_restrict_diagnostic_tools_drops_unknown_and_write_tools():
    """
    过滤后只保留白名单工具，并保持原始顺序，插件新增的工具默认被剔除。
    """
    tools = [
        SimpleNamespace(name="read_log"),
        SimpleNamespace(name="write_file"),
        SimpleNamespace(name="plugin_dynamic_write"),
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="update_plan"),
    ]

    kept = MoviePilotAgent._restrict_diagnostic_tools(tools)

    assert [tool.name for tool in kept] == ["read_log", "read_file", "update_plan"]


def test_diagnostic_middleware_allowlist_excludes_write_capable_middlewares():
    """
    中间件白名单不能包含会注入写工具、执行代码或委派子代理的实现。
    """
    allowed = set(MoviePilotAgent.DIAGNOSTIC_MIDDLEWARE_TYPES)
    forbidden = {
        SkillsMiddleware,
        JobsMiddleware,
        CodeExecutionMiddleware,
        CodeCaptureMiddleware,
        MemoryMiddleware,
        LearningMiddleware,
        LearningCaptureMiddleware,
        ToolSelectorMiddleware,
        RecallMiddleware,
        MoviePilotSubAgentMiddleware,
        SubAgentTaskControlMiddleware,
    }

    assert not allowed & forbidden
    assert PlanMiddleware in allowed
    assert UsageMiddleware in allowed


def test_restrict_diagnostic_middlewares_keeps_allowlisted_instances():
    """
    过滤按实例类型判断，白名单外的中间件一律不装配。
    """

    class _ForeignMiddleware:
        """
        模拟白名单之外的中间件，仅用于类型过滤断言。
        """

    allowed_instance = UsageMiddleware()
    plan_instance = PlanMiddleware()
    middlewares = [allowed_instance, _ForeignMiddleware(), plan_instance, object()]

    kept = MoviePilotAgent._restrict_diagnostic_middlewares(middlewares)

    assert kept == [allowed_instance, plan_instance]


def test_agent_reuse_mismatch_detects_diagnostic_switch():
    """
    同一会话在普通与只读模式间切换时必须重建 Agent，避免复用带写工具的实例。
    """
    normal_agent = SimpleNamespace(user_id="user-1", diagnostic_mode=False)
    diagnostic_agent = SimpleNamespace(user_id="user-1", diagnostic_mode=True)
    normal_task = _MessageTask(session_id="session-1", user_id="user-1", message="排查日志")
    diagnostic_task = _MessageTask(
        session_id="session-1", user_id="user-1", message="排查日志", diagnostic=True
    )

    assert _agent_reuse_mismatch(normal_agent, diagnostic_task) is True
    assert _agent_reuse_mismatch(diagnostic_agent, normal_task) is True
    assert _agent_reuse_mismatch(normal_agent, normal_task) is False
    assert _agent_reuse_mismatch(diagnostic_agent, diagnostic_task) is False


def test_agent_reuse_mismatch_detects_user_switch():
    """
    不同用户之间不能复用同一 Agent 实例。
    """
    agent = SimpleNamespace(user_id="user-1", diagnostic_mode=False)
    task = _MessageTask(session_id="session-1", user_id="user-2", message="继续")

    assert _agent_reuse_mismatch(agent, task) is True
