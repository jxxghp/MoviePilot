"""终端句柄归属、明确共享和关闭竞态的真实进程验收。"""

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.agent.terminal import manager as terminal
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.ownership import (
    TerminalAccessError,
    TerminalScope,
    bind_terminal_scope,
    close_terminal_scope,
    current_terminal_scope,
    require_terminal_scope,
)


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[_TerminalSessionManager]:
    """每例仅拥有自己创建的进程；断言失败同样回收测试资源。"""
    instance = _TerminalSessionManager()
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.close(), 15)


def _scope(user: str = "alice", task: str = "task") -> TerminalScope:
    """相同可见字段仍创建不同的宿主任务代次。"""
    return TerminalScope(user_id=user, task_id=task, kind="test")


async def _start(manager: _TerminalSessionManager, owner: TerminalScope, *, use_pty: bool = False) -> dict[str, Any]:
    """启动受控程序，以真实输出 READY 确认它正在等待唯一输入。"""
    code = (
        "import sys\n"
        + ("import tty;tty.setraw(0)\n" if use_pty else "")
        + "print('PRIVATE_READY',flush=True)\ndata=sys.stdin.buffer.read(1)\n"
        + "print('GOT:'+str(data[0]),flush=True)\n"
    )
    args = [sys.executable, "-u", "-c", code]
    command = subprocess.list2cmdline(args) if os.name == "nt" else "exec " + shlex.join(args)
    options = {"shell": "/bin/sh", "login": False} if os.name == "posix" else {}
    with bind_terminal_scope(owner):
        payload = await manager.start(command=command, use_pty=use_pty, yield_time_ms=10000, **options)
    assert "PRIVATE_READY" in payload["output"]
    return payload


async def _finish(manager: _TerminalSessionManager, owner: TerminalScope, handle: str) -> str:
    """只消费工具交付游标，确认真实进程读取字节后退出。"""
    with bind_terminal_scope(owner):
        payload = await manager.write(session_id=handle, input_text="Z")
        output = payload["output"]
        async with asyncio.timeout(5):
            while not payload["output_complete"]:
                payload = await manager.wait(
                    session_id=handle, since_seq=payload["output_until_seq"],
                    since_offset=payload["output_until_offset"], timeout_ms=1000,
                )
                output += payload["output"]
    assert payload["exit_code"] == 0
    return output


@pytest.mark.asyncio
@pytest.mark.parametrize("use_pty", [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "posix", reason="PTY 仅 POSIX"))])
@pytest.mark.parametrize("other_user", ["alice", "bob"])
async def test_real_handle_is_not_authority(manager: _TerminalSessionManager, use_pty: bool, other_user: str) -> None:
    """同名任务的新代次和另一用户均不能通过真实句柄读取、投递输入或控制进程。"""
    owner, outsider = _scope(), _scope(other_user)
    payload = await _start(manager, owner, use_pty=use_pty)
    handle = payload["session_id"]
    operations = [
        (manager.read, {}), (manager.wait, {"timeout_ms": 0}),
        (manager.write, {"input_text": "BAD"}),
        (manager.write, {"input_text": "", "close_stdin": True}),
        (manager.interrupt, {}), (manager.kill, {}),
    ]
    with bind_terminal_scope(outsider):
        for operation, options in operations:
            with pytest.raises(TerminalAccessError) as actual:
                await operation(session_id=handle, **options)
            with pytest.raises(TerminalAccessError) as missing:
                await operation(session_id="term_missing", **options)
            assert str(actual.value) == str(missing.value)
            assert "PRIVATE_READY" not in str(actual.value)
    assert manager._sessions[handle].status == "running"
    assert "GOT:90" in await _finish(manager, owner, handle)


@pytest.mark.asyncio
async def test_identity_and_empty_scope_are_explicit(manager: _TerminalSessionManager) -> None:
    """绑定空上下文允许非终端调用，终端要求可信用户、任务和未封口代次。"""
    outer = _scope()
    with bind_terminal_scope(outer):
        with bind_terminal_scope(None):
            assert current_terminal_scope() is None
            with pytest.raises(TerminalAccessError):
                require_terminal_scope()
        assert require_terminal_scope() is outer
    for owner in [_scope(user=""), _scope(task=""), outer]:
        if owner is outer:
            owner.seal()
        with bind_terminal_scope(owner), pytest.raises(TerminalAccessError):
            await manager.start(command="echo must-not-start")
    assert not manager._sessions and manager._starting == 0


@pytest.mark.asyncio
async def test_share_is_atomic_specific_and_not_transitive(manager: _TerminalSessionManager) -> None:
    """被授予子任务只可读取明确句柄，无效批量不产生部分授权且不能转授给兄弟。"""
    owner, child, sibling = _scope(), _scope(task="child"), _scope(task="sibling")
    handle = (await _start(manager, owner))["session_id"]
    with bind_terminal_scope(owner), pytest.raises(TerminalAccessError):
        manager.share(owner, child, {handle: frozenset({"read"}), "missing": frozenset({"read"})})
    assert child not in manager._grants
    with bind_terminal_scope(owner):
        manager.share(owner, child, {handle: frozenset({"read", "wait"})})
        with pytest.raises(TerminalAccessError):
            manager.share(owner, _scope("bob"), {handle: frozenset({"read"})})
        with pytest.raises(TerminalAccessError):
            manager.share(owner, sibling, {handle: frozenset({"start"})})
    with bind_terminal_scope(child):
        assert "PRIVATE_READY" in (await manager.read(session_id=handle))["output"]
        with pytest.raises(TerminalAccessError):
            await manager.write(session_id=handle, input_text="BAD")
        with pytest.raises(TerminalAccessError):
            manager.share(child, sibling, {handle: frozenset({"read"})})
    assert await manager.close_owner(child)
    assert manager._sessions[handle].status == "running"
    assert "GOT:90" in await _finish(manager, owner, handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_parent", [False, True])
async def test_long_wait_wakes_when_either_scope_seals(manager: _TerminalSessionManager, close_parent: bool) -> None:
    """等待无输出时封住任一端，不必等满一分钟才反馈授权失效。"""
    owner, child = _scope(), _scope(task="child")
    payload = await _start(manager, owner)
    with bind_terminal_scope(owner):
        manager.share(owner, child, {payload["session_id"]: frozenset({"wait"})})
    with bind_terminal_scope(child):
        pending = asyncio.create_task(manager.wait(
            session_id=payload["session_id"], timeout_ms=60000,
            since_seq=payload["output_until_seq"], since_offset=payload["output_until_offset"],
        ))
    await asyncio.sleep(0)
    (owner if close_parent else child).seal()
    with pytest.raises(TerminalAccessError):
        await asyncio.wait_for(pending, 1)


@pytest.mark.asyncio
async def test_queued_input_rechecks_grant_after_lock(manager: _TerminalSessionManager) -> None:
    """排队写入在取得输入锁之前已撤销时，不向父进程投递任何字节。"""
    owner, child = _scope(), _scope(task="child")
    handle = (await _start(manager, owner))["session_id"]
    session = manager._sessions[handle]
    with bind_terminal_scope(owner):
        manager.share(owner, child, {handle: frozenset({"write"})})
    await session.input_lock.acquire()
    with bind_terminal_scope(child):
        pending = asyncio.create_task(manager.write(session_id=handle, input_text="BAD", close_stdin=True))
    await asyncio.sleep(0)
    assert await manager.close_owner(child)
    session.input_lock.release()
    with pytest.raises(TerminalAccessError):
        await pending
    assert not session.stdin_closed and "GOT:90" in await _finish(manager, owner, handle)


@pytest.mark.asyncio
async def test_close_owner_during_start_retains_other_owner(manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动已预留但尚未登记时关闭任务，迟到进程被回收且另一个任务仍可继续。"""
    owner, other = _scope(), _scope(task="other")
    other_handle = (await _start(manager, other))["session_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._start_pipe_session

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        """把真实进程创建卡在预留之后，提供确定性的关闭交错。"""
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "_start_pipe_session", delayed)
    pending = asyncio.create_task(_start(manager, owner))
    await entered.wait()
    closing = asyncio.create_task(manager.close_owner(owner))
    await asyncio.sleep(0)
    assert owner.closed and not closing.done()
    assert "GOT:90" in await _finish(manager, other, other_handle)
    release.set()
    with pytest.raises(TerminalAccessError):
        await pending
    assert await closing
    assert not manager._owner_starts and manager._starting == 0
    assert all(session.owner is other for session in manager._sessions.values())


@pytest.mark.asyncio
async def test_cancelled_start_keeps_reservation_until_process_reaped(manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """两次取消首次等待也不会丢失尚未返回的进程，清理等待启动完成后才释放预留。"""
    owner = _scope()
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._start_pipe_session
    created = []

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        """记录实际已创建的子进程后，阻止句柄提前返回给 start。"""
        session = await original(*args, **kwargs)
        created.append(session)
        entered.set()
        await release.wait()
        return session

    monkeypatch.setattr(manager, "_start_pipe_session", delayed)
    pending = asyncio.create_task(_start(manager, owner))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert manager._starting == 1 and not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not manager._sessions and manager._starting == 0
    assert created[0].process.returncode is not None


@pytest.mark.asyncio
async def test_close_nonconvergence_preserves_record_for_retry(manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾任务尚未结束不能抹除 owner；重试在进程真实收尾后才确认完成。"""
    owner = _scope()
    handle = (await _start(manager, owner))["session_id"]
    session = manager._sessions[handle]
    with monkeypatch.context() as patch:
        patch.setattr(manager, "_wait_for_exit", AsyncMock(return_value=False))
        patch.setattr(manager, "_send_signal", lambda *_args: None)
        assert not await manager.close_owner(owner)
    assert manager._sessions[handle] is session and session.owner is owner and owner.closed
    assert session.status == "running"
    assert await manager.close_owner(owner)
    assert session.process.returncode is not None and handle not in manager._sessions


@pytest.mark.asyncio
async def test_scope_cleanup_is_lazy_and_new_manager_rejects_old_handle(manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """无终端作用域清理不加载实现，重建后的管理器不会凭旧句柄接管或重跑进程。"""
    unused = _scope(task="unused")
    with monkeypatch.context() as patch:
        patch.delitem(sys.modules, "app.agent.terminal.manager")
        assert await close_terminal_scope(unused)
        assert "app.agent.terminal.manager" not in sys.modules
    assert unused.closed
    owner = _scope()
    handle = (await _start(manager, owner))["session_id"]
    fresh = _TerminalSessionManager()
    with bind_terminal_scope(owner), pytest.raises(TerminalAccessError):
        await fresh.read(session_id=handle)
    assert not fresh._sessions
    monkeypatch.setattr(terminal, "terminal_session_manager", manager)
    assert await close_terminal_scope(owner)
    assert handle not in manager._sessions
