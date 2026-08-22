import asyncio
import os
import shlex
import subprocess
import sys

import pytest

from app.agent.middleware.subagents import SubAgentTaskControlMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy import (
    AuthSource,
    PrincipalType,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.orchestrator import DEFAULT_TOOL_POLICY_ORCHESTRATOR
from app.agent.tools.impl._terminal_session import _TerminalSessionManager


def _policy_context() -> ToolPolicyContext:
    """构造策略观测所需的最小宿主上下文。"""
    return ToolPolicyContext(
        session_id="session-1",
        user_id="user-1",
        origin=ToolOrigin.OPERATOR_DIRECT,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.INTERNAL,
        agent_context={"is_admin": True},
    )


def _shell_command(code: str) -> str:
    """构造跨平台的短生命周期 Python 子进程命令。"""
    args = [sys.executable, "-c", code]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(arg) for arg in args)


def test_timeout_marks_unknown_external_state_for_write_tools() -> None:
    """写类工具超时后必须明确提示外部状态可能仍在继续。"""
    tool = type("DynamicWriteTool", (), {"name": "plugin_write", "args_schema": None})()
    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_policy_context(),
        tool=tool,
        arguments={},
    )

    receipt = DEFAULT_TOOL_POLICY_ORCHESTRATOR.fail(
        observation,
        TimeoutError("tool timeout"),
    )

    assert receipt.external_may_continue is True
    assert receipt.needs_reconcile is True


def test_timeout_does_not_mark_safe_reads_for_reconciliation() -> None:
    """只读工具超时不应伪造外部副作用终态。"""
    tool = type("SafeReadTool", (), {"name": "query_personas", "args_schema": None})()
    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_policy_context(),
        tool=tool,
        arguments={},
    )

    receipt = DEFAULT_TOOL_POLICY_ORCHESTRATOR.fail(
        observation,
        TimeoutError("tool timeout"),
    )

    assert receipt.external_may_continue is False
    assert receipt.needs_reconcile is False


@pytest.mark.anyio
async def test_terminal_manager_close_terminates_running_pipe_session() -> None:
    """应用关闭时终端管理器必须终止仍在运行的管道进程。"""
    manager = _TerminalSessionManager()
    payload = await manager.start(
        command=_shell_command("import time; time.sleep(30)"),
        use_pty=False,
    )
    session = manager.get_session(payload["session_id"])

    await manager.close()

    assert session.process is not None
    assert session.process.returncode is not None
    assert session.status == "killed"
    assert manager._sessions == {}


@pytest.mark.anyio
async def test_agent_cleanup_closes_subagent_middlewares() -> None:
    """会话资源清理必须覆盖脱离当前回合的 subagent 控制器。"""
    agent = MoviePilotAgent(session_id="session-1", user_id="user-1")
    closed = []

    class _Middleware:
        async def close(self) -> None:
            closed.append(True)

    agent._subagent_middlewares = (_Middleware(),)

    await agent.cleanup()

    assert closed == [True]
    assert agent._subagent_middlewares == ()


def test_subagent_control_middleware_close_is_idempotent() -> None:
    """子代理控制器的全局关闭路径应可重复调用。"""
    middleware = object.__new__(SubAgentTaskControlMiddleware)
    middleware._tasks = {}

    async def _close() -> None:
        await middleware.close()
        await middleware.close()

    asyncio.run(_close())
    assert middleware._tasks == {}
