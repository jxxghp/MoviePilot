"""终端输出分页、游标校验与有界结果投影。"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agent.terminal.session import _TerminalSession

TERMINAL_DEFAULT_READ_BYTES = 10 * 1024
TERMINAL_MAX_READ_BYTES = 64 * 1024

class TerminalOutputError(ValueError):
    """携带稳定错误码和最小页预算的可恢复输出读取错误。"""

    def __init__(self, message: str, *, code: str = "invalid_output_cursor", minimum_read_bytes: Optional[int] = None) -> None:
        """保留结构化恢复提示，调用方不必解析中文消息。"""
        super().__init__(message)
        self.code = code
        self.minimum_read_bytes = minimum_read_bytes



def validate_output_budget(max_output_chars: Optional[int]) -> None:
    """宿主内部预算必须容纳有界命令预览和完整恢复元数据。"""
    if max_output_chars is not None and (type(max_output_chars) is not int or max_output_chars < 4096):
        raise ValueError("max_output_chars 必须为空或至少 4096 的整数")



def normalize_read_limit(max_bytes: Optional[int]) -> int:
    """限制单次读取返回的输出大小。"""
    try:
        normalized = int(max_bytes or TERMINAL_DEFAULT_READ_BYTES)
    except (TypeError, ValueError):
        normalized = TERMINAL_DEFAULT_READ_BYTES
    if normalized <= 0:
        return TERMINAL_DEFAULT_READ_BYTES
    return min(normalized, TERMINAL_MAX_READ_BYTES)



def resolve_cursor(
    session: _TerminalSession, *, since_seq: Optional[int], since_offset: Optional[int],
) -> tuple[int, int, bool]:
    """保留旧序号含义，验证下一分片偏移，并显式恢复已过保留窗口的游标。"""
    for name, value in (("since_seq", since_seq), ("since_offset", since_offset)):
        if value is not None and (type(value) is not int or value < 0):
            raise TerminalOutputError(f"{name} 必须为非负整数")
    if since_seq is None and since_offset:
        raise TerminalOutputError("非零 since_offset 必须同时提供 since_seq")
    seq = session.retained_from_seq - 1 if since_seq is None else since_seq
    offset = since_offset or 0
    if seq > session.next_seq - 1:
        raise TerminalOutputError("since_seq 超过当前输出高水位")
    if seq < session.retained_from_seq - 1:
        return session.retained_from_seq - 1, 0, True
    if not offset:
        return seq, 0, session.output_lost
    chunk = next((item for item in session.chunks if item.seq == seq + 1), None)
    if chunk is None or offset > chunk.byte_size:
        raise TerminalOutputError("since_offset 超出下一输出分片")
    try:
        chunk.text.encode("utf-8")[:offset].decode("utf-8")
    except UnicodeDecodeError as error:
        raise TerminalOutputError("since_offset 必须位于完整 UTF-8 字符边界") from error
    if offset == chunk.byte_size:
        return chunk.seq, 0, session.output_lost
    return seq, offset, session.output_lost



def _slice_output(encoded: bytes, limit: int, *, partial: bool) -> bytes:
    """遵守页预算和完整字符边界；零进展明确报错，不能伪装成成功分页。"""
    if len(encoded) <= limit:
        return encoded
    if not partial:
        raise TerminalOutputError(
            "当前页无法容纳完整分片；增大 max_bytes 或传 since_offset=0 后继续 read",
            code="read_limit_too_small", minimum_read_bytes=len(encoded),
        )
    text = encoded[:limit].decode("utf-8", errors="ignore")
    if not text:
        minimum = len(encoded.decode("utf-8")[0].encode("utf-8"))
        raise TerminalOutputError(
            "当前页无法容纳下一个完整 UTF-8 字符；增大 max_bytes 后继续 read",
            code="read_limit_too_small", minimum_read_bytes=minimum,
        )
    return text.encode("utf-8")



def collect_output(
    session: _TerminalSession,
    *,
    since_seq: Optional[int],
    since_offset: Optional[int] = None,
    max_bytes: Optional[int],
) -> dict[str, Any]:
    """按完整分片序号及下一分片字节偏移返回实际交付的输出页。"""
    read_limit = normalize_read_limit(max_bytes)
    seq, offset, lost = resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
    selected_chunks = [chunk for chunk in session.chunks if chunk.seq > seq]
    output_parts: list[str] = []
    output_bytes = 0
    for chunk in selected_chunks:
        encoded = chunk.text.encode("utf-8")[offset:]
        remaining = read_limit - output_bytes
        if remaining == 0:
            break
        try:
            piece = _slice_output(encoded, remaining, partial=since_offset is not None)
        except TerminalOutputError:
            if not output_parts:
                raise
            break
        output_parts.append(piece.decode("utf-8"))
        output_bytes += len(piece)
        if len(piece) < len(encoded):
            offset += len(piece)
            break
        seq, offset = chunk.seq, 0
    return {
        "output": "".join(output_parts), "output_until_seq": seq, "output_until_offset": offset,
        "output_truncated": lost or seq < session.next_seq - 1, "output_lost": lost,
    }


def read_payload(
    session: _TerminalSession, *, since_seq: Optional[int], since_offset: Optional[int],
    max_bytes: Optional[int], preserve_output_error: bool = False,
    max_output_chars: Optional[int] = None, extra_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """动作已发生时保留会话句柄和未消费游标，只把分页错误作为附加恢复信息。"""
    validate_output_budget(max_output_chars)
    try:
        page = collect_output(session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes)
    except TerminalOutputError as error:
        if not preserve_output_error or error.code != "read_limit_too_small":
            raise
        seq, offset, lost = resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        page = {
            "output": "", "output_until_seq": seq, "output_until_offset": offset,
            "output_truncated": True, "output_lost": lost,
            "output_error": {
                "code": error.code, "message": str(error), "minimum_read_bytes": error.minimum_read_bytes,
            },
        }
    payload = {**_session_payload(session, page), **(extra_fields or {})}
    if max_output_chars is None or len(json.dumps(payload, ensure_ascii=False, indent=2)) <= max_output_chars:
        return payload
    low, high = 1, normalize_read_limit(max_bytes) - 1
    best = None
    while low <= high:
        middle = (low + high) // 2
        try:
            candidate = read_payload(
                session, since_seq=since_seq, since_offset=since_offset, max_bytes=middle, extra_fields=extra_fields,
            )
        except TerminalOutputError:
            low = middle + 1
            continue
        if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= max_output_chars:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    if best is not None:
        return best
    budget_error = TerminalOutputError(
        "Agent 结果预算无法容纳完整分片；传 since_offset=0 并调整 max_bytes 后继续 read，勿重复执行动作",
        code="read_limit_too_small",
    )
    if not preserve_output_error:
        raise budget_error
    payload = read_payload(
        session, since_seq=since_seq, since_offset=since_offset, max_bytes=1,
        preserve_output_error=True, extra_fields=extra_fields,
    )
    payload["output_error"]["message"] = str(budget_error)
    return payload



def _text_preview(value: str, limit: int = 1024) -> str:
    """按 JSON 实际转义开销限制元数据预览，极长命令不能挤掉输出游标。"""
    low, high = 0, min(len(value), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(value[:middle], ensure_ascii=False)) <= limit:
            low = middle
        else:
            high = middle - 1
    return value[:low]



def _session_payload(
    session: _TerminalSession,
    page: dict[str, Any],
) -> dict[str, Any]:
    """生成工具返回的结构化会话状态。"""
    command = _text_preview(session.command)
    cwd = _text_preview(session.cwd)
    error = _text_preview(session.error, 512) if session.error else session.error
    shell = session.shell_policy.executable if session.shell_policy else None
    shell_preview = _text_preview(shell, 256) if shell else shell
    if session.status == "running":
        outcome = "pending"
    elif session.status == "error":
        outcome = "failed"
    elif session.exit_code is None:
        outcome = "unknown"
    else:
        outcome = "succeeded" if session.exit_code == 0 and session.status != "killed" else "failed"
    return {
        "session_id": session.session_id,
        "command": command, "command_truncated": command != session.command,
        "command_total_chars": len(session.command), "cwd": cwd, "cwd_truncated": cwd != session.cwd,
        "pid": session.pid,
        "status": session.status,
        "exit_code": session.exit_code,
        "execution_outcome": outcome,
        "use_pty": session.use_pty,
        "shell": shell_preview, "shell_truncated": shell_preview != shell,
        "login": session.shell_policy.login if session.shell_policy else None, "stdin_closed": session.stdin_closed,
        "last_seq": session.next_seq - 1,
        "retained_from_seq": session.retained_from_seq,
        "output_complete": session.output_complete,
        "error": error, "error_truncated": error != session.error,
        **page,
    }
