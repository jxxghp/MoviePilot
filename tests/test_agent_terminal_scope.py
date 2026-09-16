"""验证真实 Agent 与会话 worker 的终端归属装配；模型、外部服务均停在调用边界。"""

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from app.agent import orchestrator, session
from app.agent.manager import AgentManager
from app.agent.orchestrator import MoviePilotAgent
from app.agent.session import AgentManagerUnavailableError, _MessageTask
from app.agent.terminal import manager as terminal
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.ownership import (
    TerminalAccessError,
    TerminalScope,
    bind_terminal_scope,
    current_terminal_scope,
    require_terminal_scope,
)


@pytest.fixture
def closed_scopes(monkeypatch) -> list[TerminalScope]:
    """只替换进程收口边界，保留真实的宿主 scope 和 worker 生命周期。"""
    closed = []

    async def close(scope: TerminalScope) -> bool:
        """记录真实对象的封口，避免测试触及其他测试持有的全局终端。"""
        scope.seal()
        closed.append(scope)
        return True

    monkeypatch.setattr(orchestrator, "close_terminal_scope", close)
    monkeypatch.setattr(session, "close_terminal_scope", close)
    return closed


@pytest_asyncio.fixture
async def manager(closed_scopes) -> AsyncIterator[AgentManager]:
    """使用真实消息队列并为每个用例独立收口 worker，记忆边界保持无 I/O。"""
    memory = AsyncMock()
    memory.clear_memory = Mock()
    owner = AgentManager(memory=memory)
    owner._accepting_tasks = True
    try:
        yield owner
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_conversation_scope_survives_turn_and_graph_rebuild(monkeypatch, closed_scopes) -> None:
    """正常多轮、图失效均复用同一身份，调用结束恢复上层异步上下文。"""
    agent = MoviePilotAgent("conversation", user_id="alice")
    seen = []

    async def process(_message: str, **_kwargs) -> str:
        """在推理入口读取实际 ContextVar，并使出执行权检验异步传播。"""
        seen.append(require_terminal_scope())
        await asyncio.sleep(0)
        assert require_terminal_scope() is seen[-1]
        return "完成"

    monkeypatch.setattr(agent, "_process", process)
    outer = TerminalScope("operator", "parent", "operator")
    with bind_terminal_scope(outer):
        assert await agent.process("启动") == "完成"
        assert current_terminal_scope() is outer
        assert await agent._invalidate_cached_agent() is True
        assert await agent.process("继续") == "完成"
        assert current_terminal_scope() is outer
    assert seen == [agent._terminal_scope, agent._terminal_scope]
    assert not closed_scopes
    assert await agent.cleanup() is True
    with pytest.raises(TerminalAccessError):
        await agent.process("清理后不得重新启动")


@pytest.mark.asyncio
async def test_no_user_agent_can_reason_but_cannot_enter_terminal(monkeypatch) -> None:
    """无可信用户的内部 Agent 可执行非终端逻辑，不能获得默认 api_user 身份。"""
    agent = MoviePilotAgent("anonymous")

    async def process(_message: str, **_kwargs) -> str:
        """在真正的 wrapper 中验证缺身份拒绝点。"""
        assert current_terminal_scope().user_id == ""
        with pytest.raises(TerminalAccessError):
            require_terminal_scope()
        return "无需终端"

    monkeypatch.setattr(agent, "_process", process)
    assert await agent.process("纯推理") == "无需终端"
    with pytest.raises(TerminalAccessError):
        await agent.process("冒用", terminal_scope=TerminalScope("alice", "run", "scheduled"))


@pytest.mark.asyncio
async def test_cleanup_seals_all_scopes_and_retries_both_resource_owners(monkeypatch) -> None:
    """子代理清理失败不能跳过终端；终端未收敛则保持精确归属对象供重试。"""
    agent = MoviePilotAgent("conversation", user_id="alice")
    scheduled = TerminalScope("alice", "run-1", "scheduled")
    agent._scheduled_terminal_scopes.add(scheduled)
    children = AsyncMock(side_effect=[False, True, True])
    monkeypatch.setattr(agent, "_invalidate_cached_agent", children)
    calls = []

    async def close(scope: TerminalScope) -> bool:
        """检查任何第一次异步清理前全部身份均已同步封口。"""
        assert agent._terminal_scope.closed and scheduled.closed
        calls.append(scope)
        return scope is agent._terminal_scope or children.await_count == 3

    monkeypatch.setattr(orchestrator, "close_terminal_scope", close)
    assert await agent.cleanup() is False
    assert calls == [agent._terminal_scope, scheduled]
    assert scheduled in agent._scheduled_terminal_scopes
    assert await agent.cleanup() is False
    assert await agent.cleanup() is True
    assert not agent._scheduled_terminal_scopes


@pytest.mark.asyncio
async def test_worker_keeps_conversation_and_distinguishes_scheduled_runs(
    manager, monkeypatch, closed_scopes,
) -> None:
    """真实队列相同会话的交互轮次共享 owner，两次 scheduled run 各自独立收口。"""
    seen = []

    async def process(_self, message: str, **_kwargs) -> str:
        """在生产 process wrapper 内记录当前实际任务身份。"""
        seen.append(require_terminal_scope())
        return message

    monkeypatch.setattr(MoviePilotAgent, "_process", process)
    for message, run_id in [("交互1", None), ("定时1", "run-1"), ("定时2", "run-2"), ("交互2", None)]:
        assert await manager.process_message(
            session_id="shared", user_id="alice", message=message,
            wait_for_completion=True, scheduled_run_id=run_id,
        ) == message
    assert seen[0] is seen[3]
    assert seen[1] is not seen[2] and seen[1] is not seen[0]
    assert [scope.task_id for scope in seen[1:3]] == ["run-1", "run-2"]
    assert [scope.kind for scope in seen[1:3]] == ["scheduled", "scheduled"]
    assert seen[1].closed and seen[2].closed
    assert not seen[0].closed
    assert closed_scopes == seen[1:3]
    assert not manager.active_agents["shared"]._scheduled_terminal_scopes


@pytest.mark.asyncio
async def test_cancelled_queued_run_is_sealed_and_never_enters_reasoning(manager, monkeypatch) -> None:
    """取消等待中的定时 run 不影响当前交互推理，也不能稍后从队列复活。"""
    started = asyncio.Event()
    release = asyncio.Event()
    messages = []

    async def process(_self, message: str, **_kwargs) -> str:
        """仅阻塞第一条交互请求，保留后续定时请求的真实排队窗口。"""
        messages.append(message)
        if message == "交互":
            started.set()
            await release.wait()
        return message

    monkeypatch.setattr(MoviePilotAgent, "_process", process)
    interactive = asyncio.create_task(manager.process_message(
        session_id="shared", user_id="alice", message="交互", wait_for_completion=True,
    ))
    await asyncio.wait_for(started.wait(), 2)
    scheduled = asyncio.create_task(manager.process_message(
        session_id="shared", user_id="alice", message="取消的定时", scheduled_run_id="run-1",
        wait_for_completion=True,
    ))
    await asyncio.sleep(0)
    queued = manager._session_queues["shared"]._queue[0]
    scheduled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scheduled
    assert queued.terminal_scope.closed
    assert not manager.active_agents["shared"]._terminal_scope.closed
    release.set()
    assert await interactive == "交互"
    await asyncio.wait_for(manager._session_queues["shared"].join(), 2)
    assert messages == ["交互"]


@pytest.mark.asyncio
async def test_cancel_active_wait_closes_same_scope_and_retains_failed_cleanup(
    manager, monkeypatch,
) -> None:
    """等待者取消先封活动 scope；不收敛时对象留在 Agent，worker 最终再尝试回收。"""
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []
    closes = []

    async def process(_self, _message: str, **_kwargs) -> str:
        """模拟已进入模型且稍后才结束的执行，不让 caller 取消传播到会话 worker。"""
        seen.append(require_terminal_scope())
        started.set()
        await release.wait()
        with pytest.raises(TerminalAccessError):
            require_terminal_scope()
        return "收尾"

    async def close(scope: TerminalScope) -> bool:
        """第一次终端收口失败，实际 worker 结束后再报告已收敛。"""
        scope.seal()
        closes.append(scope)
        return release.is_set()

    monkeypatch.setattr(MoviePilotAgent, "_process", process)
    monkeypatch.setattr(orchestrator, "close_terminal_scope", close)
    scheduled = asyncio.create_task(manager.process_message(
        session_id="shared", user_id="alice", message="定时", scheduled_run_id="run-1",
        wait_for_completion=True,
    ))
    await asyncio.wait_for(started.wait(), 2)
    scheduled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scheduled
    agent = manager.active_agents["shared"]
    assert seen[0].closed
    assert closes == seen
    assert seen[0] in agent._scheduled_terminal_scopes
    assert not agent._terminal_scope.closed
    release.set()
    await asyncio.wait_for(manager._session_queues["shared"].join(), 2)
    assert closes == [seen[0], seen[0]]
    assert not agent._scheduled_terminal_scopes


@pytest.mark.asyncio
async def test_switch_user_retains_old_agent_until_cleanup_converges(manager, monkeypatch) -> None:
    """同 session 换主体必须等待旧资源收口，不能只修改旧 Agent 的 user_id。"""
    old = MoviePilotAgent("shared", user_id="alice")
    manager.active_agents["shared"] = old
    cleanup = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(old, "cleanup", cleanup)
    process = AsyncMock(return_value="bob 完成")
    monkeypatch.setattr(MoviePilotAgent, "_process", process)
    task = _MessageTask(session_id="shared", user_id="bob", message="新用户")
    with pytest.raises(AgentManagerUnavailableError):
        await manager._process_message_internal(task)
    assert manager.active_agents["shared"] is old
    assert old.user_id == "alice"
    process.assert_not_awaited()
    assert await manager._process_message_internal(task) == "bob 完成"
    replacement = manager.active_agents["shared"]
    assert replacement is not old and replacement.user_id == "bob"
    assert replacement._terminal_scope is not old._terminal_scope


@pytest.mark.asyncio
async def test_real_worker_terminal_survives_stop_and_other_user_cleanup(monkeypatch) -> None:
    """真实 pipe 经生产 worker 装配 owner，停止推理保留进程，换主体清理才回收。"""
    terminals = _TerminalSessionManager()
    monkeypatch.setattr(terminal, "terminal_session_manager", terminals)
    memory = AsyncMock()
    memory.clear_memory = Mock()
    owner = AgentManager(memory=memory)
    owner._accepting_tasks = True
    ready = asyncio.Event()
    handles = []
    argv = [sys.executable, "-u", "-c", "import sys; print('READY', flush=True); sys.stdin.read()"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else "exec " + shlex.join(argv)

    async def process(_self, message: str, **_kwargs) -> str:
        """模型边界使用本地固定程序，其余执行、授权和资源回收均为真实实现。"""
        if message == "启动并继续推理":
            payload = await terminals.start(
                command=command, use_pty=False, yield_time_ms=1000, since_offset=0,
            )
            handles.append(payload["session_id"])
            ready.set()
            await asyncio.Event().wait()
        if message == "新用户":
            return "新用户就绪"
        return (await terminals.read(session_id=handles[0], since_seq=0, since_offset=0))["output"]

    monkeypatch.setattr(MoviePilotAgent, "_process", process)
    execution = asyncio.create_task(owner.process_message(
        session_id="alice-session", user_id="alice", message="启动并继续推理", wait_for_completion=True,
    ))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        alice = owner.active_agents["alice-session"]
        process_record = terminals._sessions[handles[0]]
        assert await owner.stop_current_task("alice-session") is True
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert not alice._terminal_scope.closed
        assert process_record.process.returncode is None
        assert await alice._invalidate_cached_agent() is True
        assert "READY" in await owner.process_message(
            session_id="alice-session", user_id="alice", message="读取", wait_for_completion=True,
        )
        with pytest.raises(TerminalAccessError):
            await owner.process_message(
                session_id="bob-session", user_id="bob", message="读取", wait_for_completion=True,
            )
        await owner.clear_session("bob-session", "bob")
        assert process_record.process.returncode is None
        assert await owner.process_message(
            session_id="alice-session", user_id="bob", message="新用户", wait_for_completion=True,
        ) == "新用户就绪"
        assert alice._terminal_scope.closed
        assert process_record.process.returncode is not None
        assert handles[0] not in terminals._sessions
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await owner.close()
        await terminals.close()
