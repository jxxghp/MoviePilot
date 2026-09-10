"""真实终端进程的输出分页、事件唤醒和取消回收，全部通过 stdin 握手同步。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.session import _TerminalSession

_DEADLINE = 5
_INTERACTIVE = """import sys
print('READY', flush=True)
for line in sys.stdin:
    value = line.rstrip('\\n')
    if value == 'quit':
        print('FINAL-TAIL', flush=True)
        break
    if value != 'noop':
        print(value, flush=True)
"""


def _command(code: str) -> str:
    """固定当前虚拟环境解释器，并对 shell 参数逐项转义。"""
    arguments = [sys.executable, "-u", "-c", code]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _cursor(payload: dict[str, Any]) -> dict[str, int]:
    """续传实际交付位置，不能用生产端 last_seq 替代消费游标。"""
    return {"since_seq": payload["output_until_seq"], "since_offset": payload["output_until_offset"]}


def _position(payload: dict[str, Any]) -> tuple[int, int]:
    """将公开消费位置转换为可比较的有序游标。"""
    return payload["output_until_seq"], payload["output_until_offset"]


@pytest_asyncio.fixture
async def terminal_manager() -> AsyncIterator[_TerminalSessionManager]:
    """每个用例独占 manager，失败或取消也必须回收其全部真实进程。"""
    manager = _TerminalSessionManager()
    try:
        yield manager
    finally:
        await asyncio.wait_for(manager.close(), timeout=10)


async def _start_ready(manager: _TerminalSessionManager, code: str = _INTERACTIVE) -> tuple[dict[str, Any], _TerminalSession]:
    """等待明确的 READY 输出而非猜测进程启动耗时。"""
    payload = await asyncio.wait_for(manager.start(
        command=_command(code), use_pty=False, yield_time_ms=10000, since_offset=0,
        env={"PYTHONIOENCODING": "utf-8"},
    ), timeout=_DEADLINE)
    assert "READY" in payload["output"]
    session = manager.get_session(payload["session_id"])
    assert session.process is not None and session.process.returncode is None
    return payload, session


async def _input(session: _TerminalSession, value: str) -> None:
    """控制子进程的输出时序，不调用输出收集器或推进测试游标。"""
    assert session.process is not None and session.process.stdin is not None
    session.process.stdin.write(value.encode("utf-8"))
    await session.process.stdin.drain()


def _observe_wait(monkeypatch: pytest.MonkeyPatch, session: _TerminalSession, count: int = 1) -> asyncio.Event:
    """在真正 await 变化事件的边界建立握手，防止生产者早于等待注册。"""
    entered = asyncio.Event()
    waiting = session.changed_event
    original = waiting.wait
    calls = 0

    async def wait() -> Any:
        """保留真实 Event.wait，只记录所需等待者均已进入。"""
        nonlocal calls
        calls += 1
        if calls == count:
            entered.set()
        return await original()

    monkeypatch.setattr(waiting, "wait", wait)
    return entered


def _observe_bytes(monkeypatch: pytest.MonkeyPatch, session: _TerminalSession, expected: bytes) -> asyncio.Event:
    """观测实际 OS 管道数据到达 append 边界，不假设一次 read 能收齐输出。"""
    arrived = asyncio.Event()
    received = bytearray()
    original = session.append_output

    def append(stream: str, data: bytes) -> None:
        """先执行真实追加/解码，再通知测试输入字节已被处理。"""
        original(stream, data)
        if stream == "stdout":
            received.extend(data)
            if expected in received:
                arrived.set()

    monkeypatch.setattr(session, "append_output", append)
    return arrived


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    """结束测试临时等待者，避免断言失败时留下未管理协程。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _read_visible(
    manager: _TerminalSessionManager, session_id: str, first: dict[str, Any], *, max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    """只按回包游标连续读取当前可见页，并检查每次确实取得进展。"""
    output = [first["output"]]
    payload = first
    for _ in range(3000):
        assert len(payload["output"].encode("utf-8")) <= max_bytes
        assert "\ufffd" not in payload["output"]
        assert payload["output_lost"] is False
        if not payload["output_truncated"]:
            return "".join(output), payload
        previous = _position(payload)
        payload = await manager.read(session_id=session_id, max_bytes=max_bytes, **_cursor(payload))
        assert payload["output"]
        assert _position(payload) > previous
        output.append(payload["output"])
    raise AssertionError("终端分页未在有界页数内收敛")


@pytest.mark.asyncio
async def test_start_yields_first_output_while_process_remains_alive(terminal_manager):
    """首次等待由输出提前唤醒，不等满十秒窗口，也不等待交互进程结束。"""
    payload, session = await _start_ready(terminal_manager)
    assert payload["status"] == "running"
    assert payload["exit_code"] is None
    assert payload["output_complete"] is False
    assert _position(payload) > (0, 0)
    assert session.wait_task is not None and not session.wait_task.done()


@pytest.mark.asyncio
async def test_zero_yield_and_zero_wait_never_await_output_event(monkeypatch, terminal_manager):
    """显式零值只取快照，既不消费未来输出，也不进入事件等待。"""
    original_start = terminal_manager._start_pipe_session
    event_wait = AsyncMock(side_effect=AssertionError("零窗口不得等待输出事件"))

    async def start(*args: Any, **kwargs: Any) -> _TerminalSession:
        """真实启动静默交互进程，在首次返回前装入不应被调用的等待探针。"""
        session = await original_start(*args, **kwargs)
        monkeypatch.setattr(session.changed_event, "wait", event_wait)
        return session

    monkeypatch.setattr(terminal_manager, "_start_pipe_session", start)
    payload = await asyncio.wait_for(terminal_manager.start(
        command=_command("import sys; sys.stdin.readline()"), use_pty=False, yield_time_ms=0, since_offset=0,
    ), timeout=_DEADLINE)
    assert payload["output"] == "" and _position(payload) == (0, 0)
    response = await terminal_manager.wait(session_id=payload["session_id"], timeout_ms=0, **_cursor(payload))
    assert response["wait_timeout_ms"] == 0
    assert response["status"] == "running" and response["output_complete"] is False
    assert response["output"] == "" and _position(response) == (0, 0)
    event_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_output_wakes_wait_without_process_exit(monkeypatch, terminal_manager):
    """等待已明确进入后才触发输出，证明唤醒源不是进程退出或固定超时。"""
    initial, session = await _start_ready(terminal_manager)
    entered = _observe_wait(monkeypatch, session)
    task = asyncio.create_task(terminal_manager.wait(
        session_id=session.session_id, timeout_ms=10000, **_cursor(initial),
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=_DEADLINE)
        assert not task.done()
        await _input(session, "NEW-OUTPUT\n")
        payload = await asyncio.wait_for(task, timeout=_DEADLINE)
        assert payload["output"] == "NEW-OUTPUT\n"
        assert payload["status"] == "running" and payload["output_complete"] is False
        assert session.process.returncode is None and not session.wait_task.done()
    finally:
        await _cancel_tasks([task])


@pytest.mark.asyncio
async def test_output_between_empty_snapshot_and_await_is_not_lost(monkeypatch, terminal_manager):
    """同步插入恰好发生在空快照之后的输出，等待必须消费已经触发的旧通知事件。"""
    initial, session = await _start_ready(terminal_manager)
    original_read = terminal_manager._read_payload
    inserted = False

    def read(current: _TerminalSession, **kwargs: Any) -> dict[str, Any]:
        """保留真实收集器结果，仅在唯一竞态窗口插入可观察的新分片。"""
        nonlocal inserted
        payload = original_read(current, **kwargs)
        if current is session and not inserted and not payload["output"]:
            inserted = True
            current.append_output("stdout", b"BETWEEN-CHECK-AND-WAIT\n")
        return payload

    monkeypatch.setattr(terminal_manager, "_read_payload", read)
    payload = await asyncio.wait_for(terminal_manager.wait(
        session_id=session.session_id, timeout_ms=10000, **_cursor(initial),
    ), timeout=_DEADLINE)
    assert inserted and payload["output"] == "BETWEEN-CHECK-AND-WAIT\n"
    assert payload["status"] == "running" and session.process.returncode is None


@pytest.mark.asyncio
async def test_wait_deadline_keeps_session_running_and_usable(terminal_manager):
    """分段等待到期是一次空快照，不能被解释成命令超时或终止。"""
    initial, session = await _start_ready(terminal_manager)
    expired = await terminal_manager.wait(session_id=session.session_id, timeout_ms=1, **_cursor(initial))
    assert expired["wait_timeout_ms"] == 1
    assert expired["output"] == "" and _position(expired) == _position(initial)
    assert expired["status"] == "running" and expired["exit_code"] is None
    assert not session.wait_task.done()
    await _input(session, "AFTER-WAIT-DEADLINE\n")
    resumed = await asyncio.wait_for(terminal_manager.wait(
        session_id=session.session_id, timeout_ms=10000, **_cursor(expired),
    ), timeout=_DEADLINE)
    assert resumed["output"] == "AFTER-WAIT-DEADLINE\n" and resumed["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_waiters_receive_output_independently_and_cancel_is_local(monkeypatch, terminal_manager, cancel_first):
    """一个事件可唤醒所有读者，取消其中一人不能取消另一人或进程 owner。"""
    initial, session = await _start_ready(terminal_manager)
    entered = _observe_wait(monkeypatch, session, count=2)
    tasks = [asyncio.create_task(terminal_manager.wait(
        session_id=session.session_id, timeout_ms=10000, **_cursor(initial),
    )) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=_DEADLINE)
        if cancel_first:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert not tasks[1].done()
        assert not session.wait_task.done() and session.process.returncode is None
        await _input(session, "FOR-ALL-READERS\n")
        active = tasks[1:] if cancel_first else tasks
        payloads = await asyncio.wait_for(asyncio.gather(*active), timeout=_DEADLINE)
        assert all(payload["output"] == "FOR-ALL-READERS\n" for payload in payloads)
        assert all(payload["status"] == "running" for payload in payloads)
        assert not session.wait_task.cancelled()
        assert all(not task.cancelled() for task in session.reader_tasks)
    finally:
        await _cancel_tasks(tasks)


@pytest.mark.asyncio
async def test_cancelled_initial_yield_reclaims_undelivered_session(monkeypatch, terminal_manager):
    """首次窗口内尚未交付 ID 的 start 被取消，必须撤销登记并回收真实进程。"""
    original_start = terminal_manager._start_pipe_session
    entered = asyncio.Event()
    captured = []

    async def start(*args: Any, **kwargs: Any) -> _TerminalSession:
        """保留真实启动，在静默会话首次 await 输出时发出取消握手。"""
        session = await original_start(*args, **kwargs)
        captured.append(session)
        original_wait = session.changed_event.wait

        async def wait() -> Any:
            """此时 session 已登记但 start 尚未向调用方返回。"""
            entered.set()
            return await original_wait()

        monkeypatch.setattr(session.changed_event, "wait", wait)
        return session

    monkeypatch.setattr(terminal_manager, "_start_pipe_session", start)
    task = asyncio.create_task(terminal_manager.start(
        command=_command("import sys; sys.stdin.readline()"), use_pty=False, yield_time_ms=10000, since_offset=0,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=_DEADLINE)
        session = captured[0]
        assert terminal_manager._sessions[session.session_id] is session
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=_DEADLINE)
        assert session.process.returncode is not None
        assert session.wait_task.done() and all(reader.done() for reader in session.reader_tasks)
        assert terminal_manager._sessions == {} and terminal_manager._starting == 0
    finally:
        await _cancel_tasks([task])


@pytest.mark.asyncio
async def test_start_small_legacy_page_preserves_session_handle(terminal_manager):
    """动作已启动后页预算不足只能提示 read 恢复，不能丢失已创建进程的 ID。"""
    payload = await asyncio.wait_for(terminal_manager.start(
        command=_command(_INTERACTIVE), use_pty=False, yield_time_ms=10000, max_bytes=1,
    ), timeout=_DEADLINE)
    session = terminal_manager.get_session(payload["session_id"])
    assert session.process.returncode is None and payload["status"] == "running"
    assert payload["output"] == "" and _position(payload) == (0, 0)
    assert payload["output_error"]["code"]
    assert payload["output_error"]["minimum_read_bytes"] > 1
    recovered = await terminal_manager.read(session_id=session.session_id, max_bytes=1024, **_cursor(payload))
    assert "READY" in recovered["output"] and _position(recovered) > (0, 0)


@pytest.mark.asyncio
async def test_write_returns_preexisting_output_without_advancing_past_unread_bytes(monkeypatch, terminal_manager):
    """stdin 操作的回包必须交付旧输出的小页，后续游标可完整恢复余下 Unicode。"""
    initial, session = await _start_ready(terminal_manager)
    expected = "OLD-" + "世界🙂" * 40 + "\n"
    arrived = _observe_bytes(monkeypatch, session, expected.encode("utf-8"))
    await _input(session, expected)
    await asyncio.wait_for(arrived.wait(), timeout=_DEADLINE)
    first = await terminal_manager.write(
        session_id=session.session_id, input_text="noop\n", max_bytes=17, **_cursor(initial),
    )
    assert first["written_bytes"] == len(b"noop\n")
    assert first["output"] and _position(first) > _position(initial)
    actual, last = await _read_visible(terminal_manager, session.session_id, first, max_bytes=17)
    assert actual == expected
    empty = await terminal_manager.write(session_id=session.session_id, input_text="noop\n", **_cursor(last))
    assert empty["output"] == "" and _position(empty) == _position(last)
    assert empty["status"] == "running"


@pytest.mark.asyncio
async def test_real_process_unicode_burst_reconstructs_across_small_pages(monkeypatch, terminal_manager):
    """OS 任意分片和 UTF-8 小页共同作用时，公开游标仍能逐页恢复完整显示文本。"""
    initial, session = await _start_ready(terminal_manager)
    expected = "BLOCK-" + "中文🙂ab" * 900 + "-END\n"
    arrived = _observe_bytes(monkeypatch, session, b"-END\n")
    await _input(session, expected)
    await asyncio.wait_for(arrived.wait(), timeout=_DEADLINE)
    first = await terminal_manager.read(session_id=session.session_id, max_bytes=20, **_cursor(initial))
    actual, last = await _read_visible(terminal_manager, session.session_id, first, max_bytes=20)
    assert actual == expected
    assert last["output_complete"] is False and session.process.returncode is None


@pytest.mark.asyncio
async def test_json_character_budget_recomputes_actual_consumption_cursor(monkeypatch, terminal_manager):
    """需大量 JSON 转义的真实输出仍须保住消费游标，逐页精确恢复且整个回包不超预算。"""
    unit = '\\\\""\n\n\t\t'
    expected = unit * 20000 + "ESCAPE-END\n"
    code = (
        "import sys\nprint('READY', flush=True)\nsys.stdin.readline()\n"
        f"sys.stdout.write({unit!r} * 20000 + 'ESCAPE-END\\n')\nsys.stdout.flush()\nsys.stdin.readline()\n"
    )
    initial, session = await _start_ready(terminal_manager, code)
    arrived = _observe_bytes(monkeypatch, session, b"ESCAPE-END\n")
    await _input(session, "emit\n")
    await asyncio.wait_for(arrived.wait(), timeout=_DEADLINE)
    payload = initial
    parts = []
    for _ in range(30):
        previous = _position(payload)
        payload = await terminal_manager.read(
            session_id=session.session_id, max_bytes=65536, max_output_chars=65536, **_cursor(payload),
        )
        assert len(json.dumps(payload, ensure_ascii=False, indent=2)) <= 65536
        assert len(payload["output"].encode("utf-8")) <= 65536
        assert payload["output"] and _position(payload) > previous
        assert payload["output_lost"] is False
        parts.append(payload["output"])
        if not payload["output_truncated"]:
            break
    else:
        raise AssertionError("字符预算分页未收敛")
    assert "".join(parts) == expected
    assert len(parts) >= 3
    assert len(parts[0].encode("utf-8")) < 65536
    assert session.process.returncode is None


@pytest.mark.asyncio
async def test_long_command_truncates_only_display_and_preserves_real_session(terminal_manager):
    """回显命令不能耗尽 JSON 预算，实际启动与 session 记录仍保留完整命令。"""
    code = _INTERACTIVE + "\n# " + ('comment-"\\' * 1500)
    command = _command(code)
    payload = await asyncio.wait_for(terminal_manager.start(
        command=command, use_pty=False, yield_time_ms=10000, since_offset=0, max_output_chars=4096,
    ), timeout=_DEADLINE)
    session = terminal_manager.get_session(payload["session_id"])
    assert session.command == command
    assert payload["command_truncated"] is True and payload["command_total_chars"] == len(command)
    assert len(json.dumps(payload["command"], ensure_ascii=False)) <= 1024
    assert len(json.dumps(payload, ensure_ascii=False, indent=2)) <= 4096
    assert "READY" in payload["output"] and payload["status"] == "running"


@pytest.mark.asyncio
async def test_utf8_character_split_between_real_os_reads_is_not_replaced(monkeypatch, terminal_manager):
    """前两字节已被 reader 处理后才允许子进程写后半段，强制经历真实跨 read 解码。"""
    code = """import os, sys
print('READY', flush=True)
encoded = '中🙂'.encode('utf-8')
sys.stdin.readline()
os.write(sys.stdout.fileno(), encoded[:2])
sys.stdin.readline()
os.write(sys.stdout.fileno(), encoded[2:] + b'\\n')
sys.stdin.readline()
"""
    initial, session = await _start_ready(terminal_manager, code)
    first_bytes = _observe_bytes(monkeypatch, session, "中".encode("utf-8")[:2])
    await _input(session, "first\n")
    await asyncio.wait_for(first_bytes.wait(), timeout=_DEADLINE)
    incomplete = await terminal_manager.read(session_id=session.session_id, **_cursor(initial))
    assert incomplete["output"] == "" and _position(incomplete) == _position(initial)
    await _input(session, "second\n")
    complete = await asyncio.wait_for(terminal_manager.wait(
        session_id=session.session_id, timeout_ms=10000, **_cursor(incomplete),
    ), timeout=_DEADLINE)
    assert complete["output"] == "中🙂\n" and "\ufffd" not in complete["output"]
    assert complete["status"] == "running"


@pytest.mark.asyncio
async def test_kill_of_exited_session_returns_unread_tail_pages(terminal_manager):
    """已退出会话的 kill 回包也必须使用消费游标交付尾部，不能仅返回 last_seq。"""
    initial, session = await _start_ready(terminal_manager)
    await _input(session, "quit\n")
    await asyncio.wait_for(asyncio.shield(session.wait_task), timeout=_DEADLINE)
    first = await terminal_manager.kill(session_id=session.session_id, max_bytes=4, **_cursor(initial))
    actual, last = await _read_visible(terminal_manager, session.session_id, first, max_bytes=4)
    assert actual == "FINAL-TAIL\n"
    assert last["status"] == "exited" and last["exit_code"] == 0 and last["output_complete"] is True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 进程组 TERM 时的输出尾部")
async def test_kill_delivers_preexisting_output_and_signal_handler_tail(monkeypatch, terminal_manager):
    """真实 TERM 在结束进程前产生最后文本，kill 和后续页必须同时保留旧输出与尾部。"""
    code = """import signal, sys
def terminate(_signal, _frame):
    '在终止之前明确交付最后一段标准输出。'
    print('SIGNAL-TAIL', flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
print('READY', flush=True)
for line in sys.stdin:
    print(line.rstrip('\\n'), flush=True)
"""
    initial, session = await _start_ready(terminal_manager, code)
    arrived = _observe_bytes(monkeypatch, session, b"OLD-BEFORE-KILL\n")
    await _input(session, "OLD-BEFORE-KILL\n")
    await asyncio.wait_for(arrived.wait(), timeout=_DEADLINE)
    first = await terminal_manager.kill(session_id=session.session_id, max_bytes=7, **_cursor(initial))
    actual, last = await _read_visible(terminal_manager, session.session_id, first, max_bytes=7)
    assert actual.count("OLD-BEFORE-KILL") == 1
    assert actual.count("SIGNAL-TAIL") == 1
    assert last["status"] in {"killed", "exited"} and last["output_complete"] is True
    assert session.process.returncode is not None


@pytest.mark.asyncio
async def test_exit_notice_does_not_finish_wait_before_readers_complete(monkeypatch, terminal_manager):
    """进程退出与输出完结是两个事实，空页等待必须等到读取器真正收尾。"""
    initial, session = await _start_ready(terminal_manager)
    entered_finish = asyncio.Event()
    release_finish = asyncio.Event()
    original_finish = terminal_manager._finish_reader_tasks
    tail_arrived = _observe_bytes(monkeypatch, session, b"FINAL-TAIL\n")

    async def finish(current: _TerminalSession) -> None:
        """在真实 reader 收尾边界暂停，确定性制造已退出但输出尚未完结的窗口。"""
        entered_finish.set()
        await release_finish.wait()
        await original_finish(current)

    monkeypatch.setattr(terminal_manager, "_finish_reader_tasks", finish)
    task = None
    try:
        await _input(session, "quit\n")
        await asyncio.wait_for(asyncio.gather(entered_finish.wait(), tail_arrived.wait()), timeout=_DEADLINE)
        tail = await terminal_manager.read(session_id=session.session_id, **_cursor(initial))
        assert tail["output"] == "FINAL-TAIL\n" and tail["status"] == "exited"
        assert tail["output_complete"] is False
        entered_wait = _observe_wait(monkeypatch, session)
        task = asyncio.create_task(terminal_manager.wait(
            session_id=session.session_id, timeout_ms=10000, **_cursor(tail),
        ))
        await asyncio.wait_for(entered_wait.wait(), timeout=_DEADLINE)
        assert not task.done()
        release_finish.set()
        final = await asyncio.wait_for(task, timeout=_DEADLINE)
        assert final["output"] == "" and _position(final) == _position(tail)
        assert final["output_complete"] is True
    finally:
        release_finish.set()
        await _cancel_tasks([task] if task is not None else [])
