"""真实终端输入、EOF、中断与平台信号合同；测试只拥有自身创建的进程和描述符。"""

import asyncio
import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from app.agent.shell import AgentShell
from app.agent.terminal import manager as terminal
from app.agent.terminal.manager import TerminalOutputError, _TerminalSessionManager
from app.agent.terminal.session import _TerminalSession


def _command(code: str) -> str:
    """使用当前虚拟环境，POSIX exec 让被测程序直接拥有会话进程与信号处理器。"""
    args = [sys.executable, "-u", "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else "exec " + shlex.join(args)


def _cursor(payload: dict[str, Any]) -> dict[str, int]:
    """只续传已经交付的位置，不把输入/信号回执的高水位当成消费证据。"""
    return {"since_seq": payload["output_until_seq"], "since_offset": payload["output_until_offset"]}


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[_TerminalSessionManager]:
    """每个测试拥有独立管理器，失败与取消时也终止全部测试子进程。"""
    current = _TerminalSessionManager()
    try:
        yield current
    finally:
        await asyncio.wait_for(current.close(), 10)


async def _ready(manager: _TerminalSessionManager, code: str, *, use_pty: bool = False) -> dict[str, Any]:
    """等待程序显式发布 READY，不启动用户登录 shell，也不靠固定休眠猜就绪。"""
    options = {"shell": "/bin/sh", "login": False} if os.name == "posix" else {}
    payload = await asyncio.wait_for(manager.start(
        command=_command(code), use_pty=use_pty, yield_time_ms=10000, since_offset=0, **options,
    ), 5)
    assert "READY" in payload["output"]
    return payload


async def _read_to(manager: _TerminalSessionManager, current: dict[str, Any], marker: str = "") -> tuple[str, dict[str, Any]]:
    """读到指定程序握手或输出最终收尾，超时仅用于测试失败保护。"""
    output = current["output"]
    async with asyncio.timeout(5):
        while (marker and marker not in output) or (not marker and not current["output_complete"]):
            current = await manager.wait(session_id=current["session_id"], timeout_ms=1000, **_cursor(current))
            output += current["output"]
    return output, current


@pytest.mark.asyncio
async def test_pipe_last_input_and_half_close_deliver_all_bytes(manager: _TerminalSessionManager) -> None:
    """依赖 EOF 的真实程序必须收到全部末段输入，并能继续读完最终输出。"""
    payload = await _ready(manager, "import sys,hashlib\nprint('READY',flush=True)\ndata=sys.stdin.buffer.read()\nprint('RESULT:'+str(len(data))+':'+hashlib.sha256(data).hexdigest(),flush=True)")
    text = "\x03\x04" + "完整末段😀\n" * 6000
    data = text.encode()
    empty = await manager.write(session_id=payload["session_id"], input_text="", **_cursor(payload))
    assert empty["written_bytes"] == 0 and empty["stdin_closed"] is False
    result = await manager.write(session_id=payload["session_id"], input_text=text, close_stdin=True, **_cursor(empty))
    assert result["written_bytes"] == len(data) and result["stdin_closed"] is True
    output, final = await _read_to(manager, result)
    assert f"RESULT:{len(data)}:{hashlib.sha256(data).hexdigest()}" in output
    assert final["exit_code"] == 0 and final["output_complete"] is True
    repeated = await manager.write(session_id=payload["session_id"], input_text="", close_stdin=True, **_cursor(final))
    assert repeated["written_bytes"] == 0 and repeated["stdin_closed"] is True
    with pytest.raises(RuntimeError, match="stdin 已关闭"):
        await manager.write(session_id=payload["session_id"], input_text="late", **_cursor(repeated))
    with pytest.raises(RuntimeError, match="stdin 已关闭"):
        await manager.write(session_id=payload["session_id"], input_text="", **_cursor(repeated))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="本例验证 POSIX PTY 的双向描述符")
async def test_pty_half_close_rejects_before_input_and_keeps_output_readable(manager: _TerminalSessionManager) -> None:
    """不支持的组合不能先写入 BAD，也不能通过关闭 master 破坏输出。"""
    payload = await _ready(manager, "import sys,tty\ntty.setraw(0)\nprint('READY',flush=True)\ndata=sys.stdin.buffer.read(1)\nprint('GOT:'+str(data[0]),flush=True)", use_pty=True)
    session = manager.get_session(payload["session_id"])
    descriptor = session.master_fd
    with pytest.raises(ValueError, match="PTY 不支持"):
        await manager.write(session_id=session.session_id, input_text="BAD", close_stdin=True, **_cursor(payload))
    assert session.master_fd == descriptor and session.status == "running" and not session.stdin_closed
    result = await manager.write(session_id=session.session_id, input_text="Z", **_cursor(payload))
    output, final = await _read_to(manager, result)
    assert "GOT:90" in output and final["exit_code"] == 0


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="本例验证 POSIX 非阻塞 PTY 短写和回压")
async def test_large_raw_pty_input_retries_short_writes_without_losing_tail(
    manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立控制管道暂停读者，确定性填满 PTY 后再释放，验证真实短写不会丢尾。"""
    control_read, control_write = os.pipe()
    os.set_inheritable(control_read, True)
    text = "0123456789abcdef" * 4096
    data = text.encode()
    loop = asyncio.get_running_loop()
    blocked = asyncio.Event()
    writes: list[tuple[int, int]] = []
    original_write = os.write
    task = None
    try:
        code = ("import os,sys,tty,hashlib\ntty.setraw(0)\nprint('READY',flush=True)\n"
                f"os.read({control_read},1)\nos.close({control_read})\ndata=sys.stdin.buffer.read({len(data)})\n"
                "print('RESULT:'+str(len(data))+':'+hashlib.sha256(data).hexdigest(),flush=True)")
        payload = await _ready(manager, code, use_pty=True)
        descriptor = manager.get_session(payload["session_id"]).master_fd

        def observe_write(fd: int, content: Any) -> int:
            """只观察该测试 PTY 的真实写入计数与回压，不替换内核返回值。"""
            try:
                count = original_write(fd, content)
            except BlockingIOError:
                if fd == descriptor:
                    loop.call_soon_threadsafe(blocked.set)
                raise
            if fd == descriptor:
                writes.append((len(content), count))
            return count

        monkeypatch.setattr(terminal.os, "write", observe_write)
        task = asyncio.create_task(manager.write(session_id=payload["session_id"], input_text=text, **_cursor(payload)))
        await asyncio.wait_for(blocked.wait(), 5)
        original_write(control_write, b"g")
        result = await asyncio.wait_for(task, 5)
        assert any(count < requested for requested, count in writes)
        assert result["written_bytes"] == len(data)
        output, final = await _read_to(manager, result)
        assert f"RESULT:{len(data)}:{hashlib.sha256(data).hexdigest()}" in output
        assert final["exit_code"] == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        os.close(control_read)
        os.close(control_write)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="本例验证 POSIX SIGINT 处理器")
async def test_interrupt_calls_handler_once_and_program_remains_interactive(manager: _TerminalSessionManager) -> None:
    """SIGINT 不设置 kill_requested，也不在等待后升级；处理器返回后仍能输入。"""
    code = ("import signal,sys\nsignal.signal(signal.SIGINT,lambda *_:print('INTERRUPTED',flush=True))\n"
            "print('READY',flush=True)\nfor line in sys.stdin:\n print('ECHO:'+line.strip(),flush=True)\n if line.strip()=='quit':break\n")
    payload = await _ready(manager, code)
    result = await manager.interrupt(session_id=payload["session_id"], **_cursor(payload))
    assert result["signal"] == "SIGINT" and result["signal_sent"] is True
    output, after = await _read_to(manager, result, "INTERRUPTED")
    assert output.count("INTERRUPTED") == 1
    session = manager.get_session(payload["session_id"])
    assert session.status == "running" and session.kill_requested is False
    reply = await manager.write(session_id=session.session_id, input_text="quit\n", **_cursor(after))
    output, final = await _read_to(manager, reply)
    assert "ECHO:quit" in output and final["exit_code"] == 0 and final["status"] == "exited"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["NOT_A_SIGNAL", "", 0, "0", True, False, -1, 99999, "SIG_IGN", "SIG_UNBLOCK"])
async def test_invalid_signal_has_no_os_or_kill_intent_effect(value: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """未知信号、处理器常量和无效编号必须在任何状态变化之前拒绝。"""
    current = _TerminalSessionManager()
    session = _TerminalSession(session_id="signal-validation", command="memory-only", cwd=".", pid=0, use_pty=False)
    current._sessions[session.session_id] = session
    send = Mock()
    monkeypatch.setattr(current, "_send_signal", send)
    with pytest.raises(ValueError):
        await current.kill(session_id=session.session_id, sig=value)
    send.assert_not_called()
    assert session.kill_requested is False and session.status == "running"


@pytest.mark.asyncio
async def test_concurrent_pipe_write_then_close_serializes_final_segment(
    manager: _TerminalSessionManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实管道首段尚未 drain 时，close 请求必须等待输入锁并完整交付末段。"""
    payload = await _ready(manager, "import sys\nprint('READY',flush=True)\nprint('DATA:'+sys.stdin.read(),flush=True)")
    session = manager.get_session(payload["session_id"])
    writer = session.process.stdin
    original_drain = writer.drain
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def drain() -> None:
        """通过事件放大真实 drain 的持锁窗口，不伪造实际字节交付。"""
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
        await original_drain()

    monkeypatch.setattr(writer, "drain", drain)
    first = asyncio.create_task(manager.write(session_id=session.session_id, input_text="first-", **_cursor(payload)))
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        close = asyncio.create_task(manager.write(session_id=session.session_id, input_text="last", close_stdin=True, **_cursor(payload)))
        assert session.input_lock.locked() and not session.stdin_closed
        release.set()
        await asyncio.wait_for(first, 5)
        result = await asyncio.wait_for(close, 5)
        output, final = await _read_to(manager, result)
        assert "DATA:first-last" in output and final["exit_code"] == 0
    finally:
        release.set()
        for task in (first, close):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, close) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_input_after_close_is_rejected_before_writer_receives_it() -> None:
    """关闭写端等待收尾期间，后续输入不能插入 close 与完成确认之间。"""
    current = _TerminalSessionManager()
    entered, release = asyncio.Event(), asyncio.Event()
    values: list[bytes] = []

    async def wait_closed() -> None:
        """确定性保持关闭请求持有输入锁，测试不启动虚构 PID。"""
        entered.set()
        await release.wait()

    writer = SimpleNamespace(write=values.append, drain=AsyncMock(), close=Mock(), wait_closed=wait_closed)
    session = _TerminalSession(session_id="close-race", command="memory-only", cwd=".", pid=0, use_pty=False,
                               process=SimpleNamespace(stdin=writer))
    current._sessions[session.session_id] = session
    close = asyncio.create_task(current.write(session_id=session.session_id, input_text="tail", close_stdin=True))
    late = None
    try:
        await entered.wait()
        late = asyncio.create_task(current.write(session_id=session.session_id, input_text="late"))
        release.set()
        result = await close
        with pytest.raises(RuntimeError, match="stdin 已关闭"):
            await late
        assert values == [b"tail"] and result["stdin_closed"] is True
    finally:
        release.set()
        await asyncio.gather(*(task for task in (close, late) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_input_control_preserves_cursor_validation_and_action_output_error(manager: _TerminalSessionManager) -> None:
    """非法游标先于 stdin 动作，已发生 half-close 后的小页错误保留会话和输入事实。"""
    payload = await _ready(manager, "import sys\nprint('READY',flush=True)\nprint('DATA:'+sys.stdin.read(),flush=True)")
    session = manager.get_session(payload["session_id"])
    with pytest.raises(TerminalOutputError):
        await manager.write(session_id=session.session_id, input_text="wrong", close_stdin=True, since_seq=999)
    assert session.stdin_closed is False
    result = await manager.write(session_id=session.session_id, input_text="ok", close_stdin=True, since_seq=0, max_bytes=1)
    assert result["stdin_closed"] is True and result["written_bytes"] == 2
    assert result["output_error"]["code"] == "read_limit_too_small"
    assert result["session_id"] == session.session_id


@pytest.mark.asyncio
async def test_windows_interrupt_uses_actual_break_event_without_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 控制事件与终止 API 分开，仅报告实际调用的 CTRL_BREAK_EVENT。"""
    current = _TerminalSessionManager()
    process = SimpleNamespace(send_signal=Mock(), terminate=Mock(), kill=Mock())
    session = _TerminalSession(session_id="windows-interrupt", command="memory-only", cwd=".", pid=0, use_pty=False, process=process)
    current._sessions[session.session_id] = session
    with monkeypatch.context() as scoped:
        scoped.setattr(terminal.os, "name", "nt")
        scoped.setattr(terminal.signal, "CTRL_BREAK_EVENT", 1, raising=False)
        result = await current.interrupt(session_id=session.session_id)
    process.send_signal.assert_called_once_with(1)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    assert result["signal"] == "CTRL_BREAK_EVENT" and session.kill_requested is False


@pytest.mark.asyncio
async def test_windows_missing_control_event_and_unmapped_kill_never_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少控制事件或请求未映射的信号时明确失败，不能静默执行 terminate。"""
    current = _TerminalSessionManager()
    process = SimpleNamespace(send_signal=Mock(), terminate=Mock(), kill=Mock())
    session = _TerminalSession(session_id="windows-unavailable", command="memory-only", cwd=".", pid=0, use_pty=False, process=process)
    current._sessions[session.session_id] = session
    with monkeypatch.context() as scoped:
        scoped.setattr(terminal.os, "name", "nt")
        scoped.delattr(terminal.signal, "CTRL_BREAK_EVENT", raising=False)
        with pytest.raises(NotImplementedError):
            await current.interrupt(session_id=session.session_id)
        with pytest.raises(ValueError):
            await current.kill(session_id=session.session_id, sig="INT")
        current._send_signal(session, current._resolve_signal("TERM"))
        current._send_signal(session, current._resolve_signal("KILL"))
    process.send_signal.assert_not_called()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert session.kill_requested is False


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX fork 后 exec 失败分支")
async def test_pty_exec_failure_exits_child_without_parent_logic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """以模拟 fork 子分支验证失败必须 _exit，不能登记会话或回到父事件循环。"""
    current = _TerminalSessionManager()
    policy = AgentShell(kind="sh", executable="/bin/sh", arguments=("-c",), login=False)

    class ChildExited(BaseException):
        """只用于替代不应真的退出 pytest 的 os._exit。"""

    exit_child = Mock(side_effect=ChildExited())
    monkeypatch.setattr(terminal._pty, "fork", lambda: (0, 12345))
    monkeypatch.setattr(terminal.os, "chdir", Mock())
    monkeypatch.setattr(terminal.os, "execvpe", Mock(side_effect=OSError("exec failed")))
    monkeypatch.setattr(terminal.os, "_exit", exit_child)
    nonblocking = Mock()
    monkeypatch.setattr(current, "_set_nonblocking", nonblocking)
    with pytest.raises(ChildExited):
        await current._start_pty_session("echo test", str(tmp_path), {}, shell_policy=policy)
    exit_child.assert_called_once_with(127)
    nonblocking.assert_not_called()
    assert current._sessions == {}


@pytest.mark.asyncio
async def test_pipe_shell_policy_metadata_matches_explicit_non_login_execution(manager: _TerminalSessionManager) -> None:
    """真实 pipe 命令回显由同一 AgentShell 合同选择的解释器和登录模式。"""
    payload = await _ready(manager, "import sys\nprint('READY',flush=True)\nsys.stdin.read()")
    if os.name == "posix":
        assert payload["shell"] == "/bin/sh" and payload["login"] is False
    assert manager.get_session(payload["session_id"]).shell_policy is not None
