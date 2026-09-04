import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage

from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.subagents import SubAgentTaskControlMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy.contracts import (
    AuthSource,
    PrincipalType,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.orchestrator import DEFAULT_TOOL_POLICY_ORCHESTRATOR
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.impl._terminal_session import (
    _TerminalSession,
    _TerminalSessionManager,
)


class _SlowWriteTool(MoviePilotTool):
    """模拟超时后外部写操作仍可能继续的工具。"""

    name: str = "plugin_write"
    description: str = "Test a slow write tool."

    async def run(self, **kwargs) -> str:
        """等待足够久以触发测试超时。"""
        await asyncio.sleep(1)
        return "finished"


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
    tool = type("SafeReadTool", (), {"name": "persona", "args_schema": None})()
    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_policy_context(),
        tool=tool,
        arguments={"action": "list"},
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
async def test_terminal_manager_close_waits_for_starting_session() -> None:
    """关闭必须接管已经获准但尚未登记的终端启动。"""
    manager = _TerminalSessionManager()
    start_entered = asyncio.Event()
    allow_start = asyncio.Event()
    session = _TerminalSession(
        session_id="term-starting",
        command="sleep",
        cwd=".",
        pid=12345,
        use_pty=False,
    )

    async def _start_session(*_args) -> _TerminalSession:
        start_entered.set()
        await allow_start.wait()
        return session

    manager._start_pipe_session = _start_session
    manager._terminate_session = AsyncMock()

    start_task = asyncio.create_task(manager.start(command="sleep", use_pty=False))
    await start_entered.wait()
    close_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0)

    assert close_task.done() is False

    allow_start.set()
    with pytest.raises(RuntimeError, match="已关闭"):
        await start_task
    await close_task

    manager._terminate_session.assert_awaited_once_with(session)
    assert manager._sessions == {}


@pytest.mark.anyio
async def test_terminal_manager_rejects_start_after_close() -> None:
    """应用关闭后的终端管理器不得重新创建外部进程。"""
    manager = _TerminalSessionManager()

    await manager.close()

    with pytest.raises(RuntimeError, match="已关闭"):
        await manager.start(command="echo closed", use_pty=False)


@pytest.mark.anyio
async def test_terminal_manager_cancellation_terminates_unregistered_session() -> None:
    """调用方取消时必须回收已经创建但尚未登记的终端进程。"""
    manager = _TerminalSessionManager()
    session_created = asyncio.Event()
    registration_locked = asyncio.Event()
    release_registration = asyncio.Event()
    termination_started = asyncio.Event()
    session = _TerminalSession(
        session_id="term-cancelled",
        command="sleep",
        cwd=".",
        pid=12345,
        use_pty=False,
    )

    async def _hold_registration_lock() -> None:
        await session_created.wait()
        async with manager._lock:
            registration_locked.set()
            await release_registration.wait()

    async def _start_session(*_args) -> _TerminalSession:
        session_created.set()
        await registration_locked.wait()
        return session

    async def _terminate_session(_session: _TerminalSession) -> None:
        termination_started.set()

    lock_holder = asyncio.create_task(_hold_registration_lock())
    manager._start_pipe_session = _start_session
    manager._terminate_session = AsyncMock(side_effect=_terminate_session)
    start_task = asyncio.create_task(manager.start(command="sleep", use_pty=False))
    await registration_locked.wait()
    await asyncio.sleep(0)

    start_task.cancel()
    await termination_started.wait()
    release_registration.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task
    await lock_holder

    manager._terminate_session.assert_awaited_once_with(session)
    assert manager._sessions == {}
    assert manager._starting == 0


@pytest.mark.anyio
async def test_langchain_timeout_records_policy_failure() -> None:
    """LangChain 工具超时必须形成不泄露异常凭据的失败消息。"""
    tool = _SlowWriteTool(session_id="session-1", user_id="user-1")
    orchestrator = MagicMock()
    orchestrator.start.side_effect = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start
    orchestrator.fail.side_effect = DEFAULT_TOOL_POLICY_ORCHESTRATOR.fail
    orchestrator.finish.side_effect = DEFAULT_TOOL_POLICY_ORCHESTRATOR.finish
    middleware = AgentPolicyMiddleware(
        context=_policy_context(),
        orchestrator=orchestrator,
        tools=[tool],
    )
    request = SimpleNamespace(
        tool=tool,
        tool_call={"id": "call-timeout", "name": tool.name, "args": {}},
    )

    async def _handler(_request):
        result = await tool._arun()
        return ToolMessage(content=result, tool_call_id="call-timeout")

    with patch("app.agent.tools.base.settings.LLM_TOOL_TIMEOUT", 0.01):
        result = await middleware.awrap_tool_call(request, _handler)

    assert "工具执行超时" in result.content
    assert "若工具包含外部写操作" in result.content
    assert "请先确认实际状态再重试" in result.content
    assert result.status == "error"
    orchestrator.fail.assert_called_once()
    orchestrator.finish.assert_not_called()


@pytest.mark.anyio
async def test_langchain_timeout_message_sanitizes_dynamic_error() -> None:
    """非宿主工具抛出的超时异常不得把凭据带入模型上下文。"""
    tool = SimpleNamespace(name="dynamic_tool")
    middleware = AgentPolicyMiddleware(
        context=_policy_context(),
        tools=[tool],
    )
    request = SimpleNamespace(
        tool=tool,
        tool_call={"id": "call-timeout", "name": tool.name, "args": {}},
    )

    async def _handler(_request):
        raise TimeoutError("Authorization: Bearer secret-value")

    result = await middleware.awrap_tool_call(request, _handler)

    assert result.status == "error"
    assert "secret-value" not in result.content


@pytest.mark.anyio
async def test_agent_cleanup_closes_subagent_middlewares() -> None:
    """会话资源清理必须覆盖脱离当前回合的 subagent 控制器。"""
    agent = MoviePilotAgent(session_id="session-1", user_id="user-1")
    closed = []

    class _Middleware:
        async def close(self) -> None:
            closed.append(True)

    agent._subagent_middlewares = (_Middleware(),)

    assert await agent.cleanup() is True

    assert closed == [True]
    assert agent._subagent_middlewares == ()


@pytest.mark.anyio
async def test_agent_cache_replacement_closes_previous_subagent_middleware() -> None:
    """Agent 图被替换时必须释放旧图持有的子代理控制器。"""
    agent = MoviePilotAgent(session_id="session-1", user_id="user-1")
    old_middleware = SimpleNamespace(close=AsyncMock())
    new_middleware = SimpleNamespace(close=AsyncMock())
    catalog = ToolCatalogSnapshot.from_tools([], plugin_revision=0, factory_revision="factory-v1")
    agent._subagent_middlewares = (old_middleware,)

    await agent._cache_agent(
        signature=("new",),
        agent=object(),
        streaming=False,
        tool_catalog=catalog,
        subagent_catalog=catalog,
        mcp_config_signature="mcp-config",
        subagent_middlewares=(new_middleware,),
    )

    old_middleware.close.assert_awaited_once()
    new_middleware.close.assert_not_awaited()
    assert agent._subagent_middlewares == (new_middleware,)


@pytest.mark.anyio
async def test_agent_execution_failure_closes_cached_subagent_middleware() -> None:
    """图执行失败失效缓存时必须同步释放该图持有的子代理控制器。"""
    agent = MoviePilotAgent(session_id="session-1", user_id="user-1")
    middleware = SimpleNamespace(close=AsyncMock())
    graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("failed")))
    agent._compiled_agent_bundle = SimpleNamespace(agent=graph)
    agent._subagent_middlewares = (middleware,)
    agent._should_stream = lambda: False
    agent._create_agent = AsyncMock(return_value=graph)
    agent._dispatch_execution_notice = AsyncMock()
    agent.stream_handler = SimpleNamespace(stop_streaming=AsyncMock(return_value=(False, "")))

    result, _ = await agent._execute_agent([])

    assert result == "智能助手执行失败，请稍后重试"
    middleware.close.assert_awaited_once()
    assert agent._compiled_agent_bundle is None
    assert agent._subagent_middlewares == ()


def test_subagent_control_middleware_close_is_idempotent() -> None:
    """子代理控制器的全局关闭路径应可重复调用。"""
    middleware = object.__new__(SubAgentTaskControlMiddleware)
    middleware._tasks = {}

    async def _close() -> None:
        assert await middleware.close() is True
        assert await middleware.close() is True

    asyncio.run(_close())
    assert middleware._tasks == {}


@pytest.mark.anyio
async def test_subagent_close_retains_stubborn_owner_until_retry() -> None:
    """子代理忽略取消时 close 返回 False，任务结束后重试才清理记录。"""
    middleware = object.__new__(SubAgentTaskControlMiddleware)
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def _ignore_first_cancel() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(_ignore_first_cancel())
    record = SimpleNamespace(
        task_id="subagent-stubborn",
        description="stubborn",
        subagent_type="general-purpose",
        task=task,
        created_at=datetime.now(),
        started_at=datetime.now(),
        finished_at=None,
    )
    middleware._tasks = {record.task_id: record}
    await asyncio.sleep(0)

    with patch(
        "app.agent.middleware.subagents.SUBAGENT_CANCEL_GRACE_SECONDS",
        0.01,
    ):
        assert await asyncio.wait_for(middleware.close(), timeout=0.2) is False

    assert cancelled.is_set()
    assert task.done() is False
    assert middleware._tasks == {record.task_id: record}
    assert task in middleware._close_cancel_requested

    release.set()
    await asyncio.wait_for(task, timeout=0.2)
    assert await middleware.close() is True
    assert middleware._tasks == {}
    assert middleware._close_cancel_requested == set()


@pytest.mark.anyio
async def test_subagent_seal_rejects_new_detached_task() -> None:
    """控制器封口后必须同步拒绝 start，且不能创建新的 asyncio Task。"""
    middleware = object.__new__(SubAgentTaskControlMiddleware)
    middleware._tasks = {}
    middleware._accepting_tasks = True
    middleware._close_cancel_requested = set()

    middleware.seal()
    payload = await middleware._control_task(
        action="start",
        description="late task",
    )

    result = json.loads(payload)
    assert result["success"] is False
    assert "正在关闭" in result["error"]
    assert middleware._tasks == {}


@pytest.mark.anyio
async def test_agent_cleanup_retains_nonconverged_subagent_middleware() -> None:
    """Agent cleanup 必须保留返回 False 的 middleware，供重复调用继续收口。"""
    agent = MoviePilotAgent(session_id="session-owner", user_id="user-owner")
    middleware = SimpleNamespace(
        seal=MagicMock(),
        close=AsyncMock(side_effect=[False, True]),
    )
    agent._subagent_middlewares = (middleware,)

    assert await agent.cleanup() is False
    assert agent._subagent_middlewares == (middleware,)

    assert await agent.cleanup() is True
    assert agent._subagent_middlewares == ()
    assert middleware.seal.call_count == 2
    assert middleware.close.await_count == 2


@pytest.mark.anyio
async def test_subagent_cancel_reports_tasks_still_stopping() -> None:
    """取消上限到达后不得把仍运行的子代理报告为取消成功。"""
    middleware = object.__new__(SubAgentTaskControlMiddleware)
    release = asyncio.Event()

    async def _ignore_first_cancel() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(_ignore_first_cancel())
    record = SimpleNamespace(
        task_id="subagent-stopping",
        description="stopping",
        subagent_type="general-purpose",
        task=task,
        created_at=datetime.now(),
        started_at=datetime.now(),
        finished_at=None,
    )
    middleware._tasks = {record.task_id: record}
    await asyncio.sleep(0)

    with patch(
        "app.agent.middleware.subagents.SUBAGENT_CANCEL_GRACE_SECONDS",
        0.01,
    ):
        payload = await middleware._control_task(
            action="cancel",
            task_id=record.task_id,
        )

    result = json.loads(payload)
    assert result["success"] is False
    assert result["cancel_pending_task_ids"] == [record.task_id]
    assert result["tasks"][0]["status"] == "running"

    release.set()
    await asyncio.wait_for(task, timeout=0.2)
