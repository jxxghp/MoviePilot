"""用独立内存会话验证终端消费游标、UTF-8、保留窗口和最终输出预算。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.agent.terminal import session as terminal_state
from app.agent.terminal.manager import TerminalOutputError, _TerminalSessionManager
from app.agent.terminal.session import _TerminalSession
from app.agent.tools.result import inspect_tool_result


def _world(*, use_pty: bool = True) -> tuple[_TerminalSessionManager, _TerminalSession]:
    """仅登记内存记录，不创建 OS 进程，也不调用终止这些虚构 PID 的动作。"""
    manager = _TerminalSessionManager()
    session = _TerminalSession(session_id="term-cursor-test", command="memory-only", cwd=".", pid=0, use_pty=use_pty)
    manager._sessions[session.session_id] = session
    return manager, session


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_requested", [False, True])
async def test_finished_session_without_exit_code_keeps_unknown_outcome(kill_requested: bool) -> None:
    """读取器收尾或等待器失去退出码均不能让背景命令被统一回执误判为成功。"""
    manager, session = _world()
    session.kill_requested = kill_requested
    session.mark_finished(None)
    session.finish_output()
    payload = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0)
    assert payload["output_complete"] is True
    assert payload["exit_code"] is None
    assert inspect_tool_result(payload).value == "unknown"


async def _pages(
    manager: _TerminalSessionManager, session: _TerminalSession, *, max_bytes: int, max_output_chars: Optional[int] = None,
) -> list[dict[str, Any]]:
    """从初始双游标取到尾部，同时约束每页确实推进且不超声明的预算。"""
    result = []
    seq, offset = 0, 0
    for _ in range(10000):
        page = await manager.read(
            session_id=session.session_id, since_seq=seq, since_offset=offset,
            max_bytes=max_bytes, max_output_chars=max_output_chars,
        )
        assert len(page["output"].encode("utf-8")) <= max_bytes
        if max_output_chars:
            assert len(json.dumps(page, ensure_ascii=False, indent=2)) <= max_output_chars
        next_cursor = page["output_until_seq"], page["output_until_offset"]
        if page["output"]:
            assert next_cursor > (seq, offset)
        result.append(page)
        seq, offset = next_cursor
        if not page["output_truncated"]:
            return result
    raise AssertionError("输出分页没有在有界次数内推进到尾部")


@pytest.mark.asyncio
async def test_partial_chunk_cursor_resumes_exactly_without_loss_or_duplicate() -> None:
    """同一大分片分成小页时必须能表达已交付的内部位置。"""
    manager, session = _world()
    text = "prefix-" + "0123456789" * 20 + "-suffix"
    session.append_output("pty", text.encode())
    pages = await _pages(manager, session, max_bytes=20)
    assert pages[0]["output_until_seq"] == 0
    assert pages[0]["output_until_offset"] == 20
    assert pages[-1]["output_until_seq"] == 1
    assert pages[-1]["output_until_offset"] == 0
    assert "".join(page["output"] for page in pages) == text


@pytest.mark.asyncio
async def test_legacy_cursor_returns_only_complete_chunks() -> None:
    """省略 offset 的旧调用不交付下一分片的一半，继续按完整分片序号推进。"""
    manager, session = _world()
    session.append_output("pty", b"12345")
    session.append_output("pty", b"abcdef")
    first = await manager.read(session_id=session.session_id, since_seq=0, max_bytes=8)
    second = await manager.read(session_id=session.session_id, since_seq=first["output_until_seq"], max_bytes=8)
    assert first["output"] == "12345"
    assert first["output_until_seq"] == 1
    assert first["output_until_offset"] == 0
    assert second["output"] == "abcdef"
    assert second["output_until_seq"] == 2
    assert second["output_truncated"] is False


@pytest.mark.asyncio
async def test_legacy_tiny_page_fails_with_explicit_recovery_budget() -> None:
    """无法容纳完整首分片时不能再次返回相同的成功前缀。"""
    manager, session = _world()
    session.append_output("pty", b"abcdefghij")
    with pytest.raises(TerminalOutputError) as failure:
        await manager.read(session_id=session.session_id, since_seq=0, max_bytes=3)
    assert failure.value.code == "read_limit_too_small"
    assert failure.value.minimum_read_bytes == 10
    page = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=3)
    assert page["output"] == "abc"
    assert page["output_until_offset"] == 3


@pytest.mark.asyncio
async def test_utf8_pages_and_stable_stream_labels_reconstruct_text() -> None:
    """标签和多字节正文共享稳定偏移，跨页拼接既不重复标签也不损伤字符。"""
    manager, session = _world(use_pty=False)
    session.append_output("stdout", "甲😀乙".encode())
    session.append_output("stdout", "续".encode())
    session.append_output("stderr", "错误".encode())
    session.append_output("stdout", "末尾".encode())
    pages = await _pages(manager, session, max_bytes=4)
    text = "".join(page["output"] for page in pages)
    assert text == "\n[标准输出]\n甲😀乙续\n[错误输出]\n错误\n[标准输出]\n末尾"
    assert "\ufffd" not in text


@pytest.mark.asyncio
async def test_utf8_character_larger_than_page_reports_minimum_without_advancing() -> None:
    """一个字符也放不下时给出明确失败，ASCII 小页仍然保持可用。"""
    manager, session = _world()
    session.append_output("pty", "😀A".encode())
    with pytest.raises(TerminalOutputError) as failure:
        await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=3)
    assert failure.value.minimum_read_bytes == 4
    page = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=4)
    assert page["output"] == "😀"
    assert page["output_until_offset"] == 4
    final = await manager.read(session_id=session.session_id, since_seq=0, since_offset=4, max_bytes=1)
    assert final["output"] == "A"
    assert (final["output_until_seq"], final["output_until_offset"]) == (1, 0)


@pytest.mark.asyncio
async def test_each_stream_incrementally_decodes_and_flushes_once() -> None:
    """stdout/stderr 不共享半字符状态，EOF 只冲洗一次真正不完整的字节。"""
    manager, session = _world(use_pty=False)
    encoded = "中".encode()
    session.append_output("stdout", encoded[:2])
    assert session.next_seq == 1
    session.append_output("stderr", b"error")
    session.append_output("stdout", encoded[2:])
    session.append_output("stdout", b"\xe4")
    session.finish_stream("stdout")
    session.finish_stream("stdout")
    session.finish_stream("stderr")
    session.finish_output()
    page = await manager.read(session_id=session.session_id)
    assert page["output"] == "\n[错误输出]\nerror\n[标准输出]\n中\ufffd"
    assert page["output_complete"] is True
    assert page["output_lost"] is False


@pytest.mark.asyncio
async def test_forced_stream_stop_marks_output_loss() -> None:
    """读取器未见 EOF 就被停止时，结果不能自称收齐了完整日志。"""
    manager, session = _world()
    session.append_output("pty", b"partial")
    session.finish_stream("pty", complete=False)
    session.finish_output()
    page = await manager.read(session_id=session.session_id)
    assert page["output"] == "partial"
    assert page["output_lost"] is True
    assert page["output_complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("seq", "offset"), [
    (-1, 0), (True, 0), (0, True), (0, -1), (2, 0), (0, 2), (0, 9), (1, 1), (None, 1),
])
async def test_invalid_cursor_rejected_without_consuming_output(seq: Any, offset: Any) -> None:
    """无效类型、未来位置和字符内部偏移不能成为合法消费位置。"""
    manager, session = _world()
    session.append_output("pty", "A中".encode())
    with pytest.raises(TerminalOutputError) as failure:
        await manager.read(session_id=session.session_id, since_seq=seq, since_offset=offset)
    assert failure.value.code == "invalid_output_cursor"
    page = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0)
    assert page["output"] == "A中"


@pytest.mark.asyncio
async def test_exact_end_offset_is_normalized_to_next_chunk() -> None:
    """恰好读到分片末尾的外部游标可规范化，但不能丢掉紧接着的分片。"""
    manager, session = _world()
    session.append_output("pty", b"first")
    session.append_output("pty", b"second")
    page = await manager.read(session_id=session.session_id, since_seq=0, since_offset=5)
    assert page["output"] == "second"
    assert (page["output_until_seq"], page["output_until_offset"]) == (2, 0)


@pytest.mark.asyncio
async def test_retention_gap_resets_partial_offset_and_reports_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    """部分读取的分片被淘汰后，重新从当前保留窗口起点读并明确历史缺口。"""
    manager, session = _world()
    monkeypatch.setattr(terminal_state, "TERMINAL_MAX_RETAINED_BYTES", 8)
    session.append_output("pty", b"first")
    first = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=2)
    session.append_output("pty", b"second")
    page = await manager.read(
        session_id=session.session_id, since_seq=first["output_until_seq"], since_offset=first["output_until_offset"],
    )
    assert page["retained_from_seq"] == 2
    assert page["output_lost"] is True
    assert page["output"] == "second"
    next_page = await manager.read(session_id=session.session_id, since_seq=page["output_until_seq"], since_offset=0)
    assert next_page["output"] == ""
    assert next_page["output_lost"] is False


@pytest.mark.asyncio
async def test_independent_readers_keep_their_own_cursor() -> None:
    """会话不保存全局消费位置，同一游标重试与不同读者互不影响。"""
    manager, session = _world()
    session.append_output("pty", b"abcdefghij")
    first = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=4)
    await manager.read(session_id=session.session_id, since_seq=0, since_offset=4, max_bytes=4)
    repeated = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=4)
    assert repeated == first


@pytest.mark.asyncio
async def test_action_output_error_preserves_handle_and_unconsumed_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次输出页过小时，启动已发生的会话仍可按 ID 继续读取，不能盲重试命令。"""
    manager, session = _world()
    manager._sessions.clear()
    session.append_output("pty", b"large-output")
    session.mark_finished(0)
    session.finish_output()

    async def start_session(*_args: Any, **_kwargs: Any) -> _TerminalSession:
        """返回独立内存会话，避免此纯分页测试创建外部进程。"""
        return session

    monkeypatch.setattr(manager, "_start_pipe_session", start_session)
    payload = await manager.start(command="echo test", cwd=str(tmp_path), use_pty=False, yield_time_ms=0, max_bytes=2)
    assert payload["session_id"] == session.session_id
    assert payload["exit_code"] == 0
    assert payload["output"] == ""
    assert (payload["output_until_seq"], payload["output_until_offset"]) == (0, 0)
    assert payload["output_error"]["code"] == "read_limit_too_small"
    assert payload["output_error"]["minimum_read_bytes"] == len(b"large-output")
    page = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0, max_bytes=2)
    assert page["output"] == "la"


@pytest.mark.asyncio
async def test_write_prevalidates_cursor_and_keeps_post_action_read_error() -> None:
    """非法游标在 stdin 动作前拒绝，写入后的页错误只报告输出恢复信息。"""
    manager, session = _world(use_pty=False)
    written: list[bytes] = []

    async def drain() -> None:
        """模拟管道接受输入，不产生任何 OS 操作。"""

    session.process = SimpleNamespace(stdin=SimpleNamespace(write=written.append, drain=drain))
    session.append_output("stdout", b"unread-output")
    with pytest.raises(TerminalOutputError):
        await manager.write(session_id=session.session_id, input_text="wrong", since_seq=9, since_offset=0)
    assert written == []
    result = await manager.write(session_id=session.session_id, input_text="ok", since_seq=0, max_bytes=1)
    assert written == [b"ok"]
    assert result["session_id"] == session.session_id
    assert result["written_bytes"] == 2
    assert result["output_error"]["code"] == "read_limit_too_small"
    assert (result["output_until_seq"], result["output_until_offset"]) == (0, 0)


@pytest.mark.asyncio
async def test_kill_of_finished_session_keeps_output_error_and_handle() -> None:
    """已结束会话的 kill 回执同样不能把未交付文本当成已消费。"""
    manager, session = _world()
    session.append_output("pty", b"unread")
    session.mark_finished(0)
    session.finish_output()
    result = await manager.kill(session_id=session.session_id, since_seq=0, max_bytes=1)
    assert result["session_id"] == session.session_id
    assert result["status"] == "exited"
    assert result["output_error"]["code"] == "read_limit_too_small"
    assert (result["output_until_seq"], result["output_until_offset"]) == (0, 0)


@pytest.mark.asyncio
async def test_json_budget_recalculates_cursor_after_escaping() -> None:
    """正文的字节预算不足以约束 JSON，实际序列化预算缩页后游标也必须同步。"""
    manager, session = _world()
    text = "\\\n\t\"" * 18000
    for start in range(0, len(text), 4096):
        session.append_output("pty", text[start:start + 4096].encode())
    pages = await _pages(manager, session, max_bytes=65536, max_output_chars=65536)
    assert len(pages[0]["output"].encode()) < 65536
    assert "".join(page["output"] for page in pages) == text


@pytest.mark.asyncio
async def test_long_metadata_and_action_error_fit_internal_json_budget() -> None:
    """命令、目录和错误回显有明确预览标记，不挤掉会话身份或恢复字段。"""
    manager, session = _world()
    session.command = "\x01\\" * 20000
    session.cwd = "\\" * 20000
    session.error = "\x01" * 20000
    original = session.command
    session.append_output("pty", ("\\" * 6000).encode())
    session.mark_finished(0)
    session.finish_output()
    result = await manager.kill(session_id=session.session_id, since_seq=0, max_bytes=65536, max_output_chars=4096)
    assert len(json.dumps(result, ensure_ascii=False, indent=2)) <= 4096
    assert result["command_truncated"] is True
    assert result["command_total_chars"] == len(original)
    assert session.command == original
    assert result["cwd_truncated"] is True
    assert result["error_truncated"] is True
    assert result["output_error"]["code"] == "read_limit_too_small"
    assert (result["output_until_seq"], result["output_until_offset"]) == (0, 0)


@pytest.mark.asyncio
async def test_zero_wait_does_not_enter_event_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式零预算只取当前快照，不被默认值替换，也不取消生产者。"""
    manager, session = _world()

    async def forbidden_wait() -> None:
        """一旦进入等待就令测试失败，不通过计时猜测是否阻塞。"""
        raise AssertionError("zero wait entered an event wait")

    monkeypatch.setattr(session.changed_event, "wait", forbidden_wait)
    result = await manager.wait(session_id=session.session_id, timeout_ms=0, since_seq=0, since_offset=0)
    assert result["wait_timeout_ms"] == 0
    assert result["wait_reason"] == "timeout"
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_capture_loss_does_not_repeatedly_wake_wait_at_consumed_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """已报告的永久捕获丢失不是新输出，读到尾部后应等其他流产生新内容。"""
    manager, session = _world()
    session.append_output("stdout", b"before")
    session.finish_stream("stdout", complete=False)
    first = await manager.read(session_id=session.session_id, since_seq=0, since_offset=0)
    entered = asyncio.Event()
    original_wait = session.changed_event.wait

    async def observed_wait() -> bool:
        """确认进入真实事件等待后才追加后续内容，不用固定休眠猜时序。"""
        entered.set()
        return await original_wait()

    monkeypatch.setattr(session.changed_event, "wait", observed_wait)
    waiting = asyncio.create_task(manager.wait(
        session_id=session.session_id, since_seq=first["output_until_seq"], since_offset=0, timeout_ms=10000,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not waiting.done()
        session.append_output("stderr", b"after")
        result = await asyncio.wait_for(waiting, timeout=1)
        assert result["output"] == "after"
        assert result["output_lost"] is True
        assert result["wait_reason"] == "output"
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_capture_loss_at_complete_tail_returns_completed() -> None:
    """所有读取器已收尾时，永久丢失标记随包报告但不能覆盖 completed 原因。"""
    manager, session = _world()
    session.finish_stream("stdout", complete=False)
    session.mark_finished(0)
    session.finish_output()
    result = await manager.wait(session_id=session.session_id, since_seq=0, since_offset=0, timeout_ms=0)
    assert result["output"] == ""
    assert result["output_lost"] is True
    assert result["output_complete"] is True
    assert result["wait_reason"] == "completed"


@pytest.mark.asyncio
async def test_new_retention_gap_wakes_once_then_updated_cursor_can_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求位置刚被保留窗口淘汰时立即报告缺口，新消费游标不会再次触发同一缺口。"""
    manager, session = _world()
    monkeypatch.setattr(terminal_state, "TERMINAL_MAX_RETAINED_BYTES", 1)
    session.append_output("pty", b"discarded")

    async def forbidden_wait() -> bool:
        """全页已被淘汰的缺口也应立即可见，不能等待不存在的正文。"""
        raise AssertionError("new retention gap entered wait")

    monkeypatch.setattr(session.changed_event, "wait", forbidden_wait)
    result = await manager.wait(session_id=session.session_id, since_seq=0, since_offset=0, timeout_ms=10000)
    assert result["output"] == ""
    assert result["output_lost"] is True
    assert result["wait_reason"] == "output"
    final = await manager.wait(
        session_id=session.session_id, since_seq=result["output_until_seq"], since_offset=0, timeout_ms=0,
    )
    assert final["wait_reason"] == "timeout"
    assert final["output_lost"] is False


def test_event_rotation_notifies_old_waiters_without_clearing_new_event() -> None:
    """每次变化发布新事件，之前捕获事件的多个等待者都能看到同一次通知。"""
    _, session = _world()
    original = session.changed_event
    session.append_output("pty", b"one")
    replacement = session.changed_event
    assert original.is_set()
    assert not replacement.is_set()
    assert replacement is not original
    session.mark_finished(0)
    assert replacement.is_set()
    assert not session.changed_event.is_set()


@pytest.mark.parametrize("value", [True, -1, 1.5, "10"])
def test_initial_yield_rejects_invalid_direct_arguments(value: Any) -> None:
    """首次等待直调边界与工具 schema 都必须拒绝非法类型。"""
    with pytest.raises(ValueError):
        _TerminalSessionManager._normalize_yield_timeout(value)


def test_initial_yield_defaults_zero_and_cap_are_explicit() -> None:
    """首屏默认短等待，零值立即返回，超大值受宿主上限约束。"""
    assert _TerminalSessionManager._normalize_yield_timeout(None) == 250
    assert _TerminalSessionManager._normalize_yield_timeout(0) == 0
    assert _TerminalSessionManager._normalize_yield_timeout(90000) == 10000
