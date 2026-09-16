"""真实命令入口必须使用宿主作用域，工具参数和内部调用者不能冒领其他终端。"""

import asyncio
import json
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.terminal import manager as terminal_module
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope, close_terminal_scope
from app.agent.tools.impl import execute_command as command_module
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.manager import MoviePilotToolsManager


def _caller() -> MoviePilotToolsManager:
    """仅替换目录/回执边界，使用真实内部调用入口、工具和终端管理器。"""
    caller = MoviePilotToolsManager(user_id="owner")
    tool = ExecuteCommandTool(session_id=caller.session_id, user_id="owner")
    caller.get_strict_tool = lambda _name: tool
    caller._ensure_policy_runtime = lambda: (
        SimpleNamespace(start=lambda **_kwargs: None), SimpleNamespace(agent_context={}),
    )
    return caller


@pytest.mark.asyncio
async def test_unbound_tool_cannot_launch_or_claim_an_owner(monkeypatch):
    """模型自行填写身份字段不能替代宿主，拒绝发生在获取进程管理器之前。"""
    create = AsyncMock()
    monkeypatch.setattr(command_module, "get_terminal_session_manager", create)
    tool = ExecuteCommandTool(session_id="conversation", user_id="owner")
    result = json.loads(await tool.run(action="start", command="echo not-started", owner="owner", task_id="task"))
    assert result["code"] == "terminal_access_denied"
    assert result["execution_outcome"] == "failed"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_internal_callers_own_distinct_terminals_and_close_independently(monkeypatch):
    """同用户的两个内部调用者不能靠真实 handle 串读，关闭 A 不终止 B。"""
    manager = _TerminalSessionManager()
    monkeypatch.setattr(terminal_module, "terminal_session_manager", manager)
    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    first, second = _caller(), _caller()
    assert first.session_id != second.session_id
    command = shlex.join([sys.executable, "-u", "-c", "print('PRIVATE-OUTPUT', flush=True); input()"])
    try:
        a = json.loads(await first.call_tool("execute_command", {"action": "start", "command": command, "use_pty": False}))
        b = json.loads(await second.call_tool("execute_command", {"action": "start", "command": command, "use_pty": False}))
        denied = json.loads(await second.call_tool("execute_command", {"action": "read", "session_id": a["session_id"]}))
        assert denied["code"] == "terminal_access_denied"
        assert "PRIVATE-OUTPUT" not in json.dumps(denied)
        assert await asyncio.wait_for(first.close(), 5)
        own = json.loads(await second.call_tool("execute_command", {"action": "read", "session_id": b["session_id"]}))
        assert own["status"] == "running"
        assert manager._sessions[b["session_id"]].process.returncode is None
        assert await asyncio.wait_for(second.close(), 5)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_nested_internal_call_keeps_current_host_identity(monkeypatch):
    """图内转调工具管理器时继承当前任务，不能静默改成内部调用者的独立身份。"""
    manager = _TerminalSessionManager()
    monkeypatch.setattr(terminal_module, "terminal_session_manager", manager)
    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    caller = _caller()
    host = TerminalScope(user_id="owner", task_id="scheduled-run-1", kind="scheduled")
    try:
        with bind_terminal_scope(host):
            result = json.loads(await caller.call_tool("execute_command", {"action": "start", "command": "echo nested", "use_pty": False}))
        assert manager._sessions[result["session_id"]].owner is host
        denied = json.loads(await caller.call_tool("execute_command", {"action": "read", "session_id": result["session_id"]}))
        assert denied["code"] == "terminal_access_denied"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_run_scope_sealed_while_waiting_for_command_slot_never_starts_process(monkeypatch):
    """一次性命令排队期间任务被关闭时，不能迟到创建子进程。"""
    scope = TerminalScope(user_id="owner", task_id="queued-run", kind="scheduled")
    slot = asyncio.Semaphore(0)
    created = AsyncMock()
    monkeypatch.setattr(command_module, "_command_semaphore", slot)
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", created)
    tool = ExecuteCommandTool(session_id="queued-run", user_id="owner")

    with bind_terminal_scope(scope):
        pending = asyncio.create_task(tool.run(action="run", command="echo never"))
    for _ in range(20):
        if scope._active_runs:
            break
        await asyncio.sleep(0)
    scope.seal()
    result = json.loads(await asyncio.wait_for(pending, 2))
    assert result["code"] == "terminal_access_denied"
    created.assert_not_awaited()
    assert await scope.wait_runs()


@pytest.mark.asyncio
async def test_run_cancelled_while_waiting_for_command_slot_releases_waiter(monkeypatch):
    """调用方取消并发槽等待时，不得留下迟到获取槽位的后台任务。"""
    scope = TerminalScope(user_id="owner", task_id="cancelled-run", kind="scheduled")
    slot = asyncio.Semaphore(0)
    created = AsyncMock()
    monkeypatch.setattr(command_module, "_command_semaphore", slot)
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", created)
    tool = ExecuteCommandTool(session_id="cancelled-run", user_id="owner")

    with bind_terminal_scope(scope):
        pending = asyncio.create_task(tool.run(action="run", command="echo never"))
    for _ in range(20):
        if scope._active_runs:
            break
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await scope.wait_runs()
    slot.release()
    await asyncio.sleep(0)
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scope_close_waits_for_real_process_and_returns_failed(monkeypatch):
    """运行中的一次性命令响应封口并真实收尾，作用域不能提前报告成功。"""
    manager = _TerminalSessionManager()
    monkeypatch.setattr(terminal_module, "terminal_session_manager", manager)
    started = asyncio.Event()
    original_create = command_module.asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        """观察真实 subprocess 创建边界，不替换子进程本身。"""
        process = await original_create(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", create)
    scope = TerminalScope(user_id="owner", task_id="running-run", kind="scheduled")
    tool = ExecuteCommandTool(session_id="running-run", user_id="owner")
    command = shlex.join([sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(30)"])
    try:
        with bind_terminal_scope(scope):
            running = asyncio.create_task(tool.run(action="run", command=command, timeout=60))
        await asyncio.wait_for(started.wait(), 5)
        closing = asyncio.create_task(close_terminal_scope(scope))
        result = json.loads(await asyncio.wait_for(running, 10))
        assert result["success"] is False
        assert result["status"] == "cancelled"
        assert result["execution_outcome"] == "failed"
        assert await asyncio.wait_for(closing, 2)
        assert scope._active_runs == 0
    finally:
        await manager.close()
