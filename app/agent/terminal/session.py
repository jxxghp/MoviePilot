"""终端会话状态、增量文本和有界输出保留。"""

from __future__ import annotations

import asyncio
import codecs
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.shell import AgentShell
from app.agent.terminal.ownership import TerminalScope

TERMINAL_RETENTION_SECONDS = 30 * 60
TERMINAL_MAX_RETAINED_BYTES = 1024 * 1024


@dataclass
class _TerminalChunk:
    """记录终端输出分片，供增量读取时按 seq 过滤。"""

    seq: int
    stream: str
    text: str
    byte_size: int
    created_at: float


@dataclass
class _TerminalSession:
    """保存一个后台命令会话的进程、输出和状态。"""

    session_id: str
    command: str
    cwd: str
    pid: int
    use_pty: bool
    shell_policy: Optional[AgentShell] = None
    owner: Optional[TerminalScope] = None
    stdin_closed: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "running"
    exit_code: Optional[int] = None
    process: Optional[asyncio.subprocess.Process] = None
    master_fd: Optional[int] = None
    chunks: list[_TerminalChunk] = field(default_factory=list)
    next_seq: int = 1
    retained_from_seq: int = 1
    retained_bytes: int = 0
    kill_requested: bool = False
    error: Optional[str] = None
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    wait_task: Optional[asyncio.Task[None]] = None
    changed_event: asyncio.Event = field(default_factory=asyncio.Event)
    output_complete: bool = False
    output_lost: bool = False
    _decoders: dict[str, Any] = field(default_factory=dict, repr=False)
    _finished_streams: set[str] = field(default_factory=set, repr=False)
    _last_stream: Optional[str] = None

    def notify_changed(self) -> None:
        """轮换事件并唤醒全部旧等待者，避免 clear 造成检查与等待之间丢失通知。"""
        previous = self.changed_event
        self.changed_event = asyncio.Event()
        previous.set()

    def append_output(self, stream: str, data: bytes) -> None:
        """每个流独立增量解码，跨 OS 读取的半个字符不会提前替换。"""
        if not data or stream in self._finished_streams:
            return
        decoder = self._decoders.setdefault(stream, codecs.getincrementaldecoder("utf-8")(errors="replace"))
        self._append_text(stream, decoder.decode(data))

    def finish_stream(self, stream: str, *, complete: bool = True) -> None:
        """流收尾时冲洗最后字符，非 EOF 收尾显式记录输出可能丢失。"""
        if stream in self._finished_streams:
            return
        self._finished_streams.add(stream)
        if not complete:
            self.output_lost = True
        decoder = self._decoders.get(stream)
        if decoder is not None:
            self._append_text(stream, decoder.decode(b"", final=True))
        self.notify_changed()

    def _append_text(self, stream: str, text: str) -> None:
        """把流标签与正文固定成不可变显示分片，游标也覆盖标签的字节。"""
        if not text:
            return
        if not self.use_pty and stream != self._last_stream:
            title = "标准输出" if stream == "stdout" else "错误输出"
            text = f"\n[{title}]\n{text}"
        self._last_stream = stream
        chunk = _TerminalChunk(
            seq=self.next_seq,
            stream=stream,
            text=text,
            byte_size=len(text.encode("utf-8")),
            created_at=time.time(),
        )
        self.next_seq += 1
        self.chunks.append(chunk)
        self.retained_bytes += chunk.byte_size
        self.updated_at = chunk.created_at
        self._trim_output()
        self.notify_changed()

    def finish_output(self) -> None:
        """读取器全部停止后发布最终边界，进程退出不能提前代表日志收齐。"""
        for stream in self._decoders:
            self.finish_stream(stream, complete=stream in self._finished_streams)
        self.output_complete = True
        self.notify_changed()

    def _trim_output(self) -> None:
        """移除超出保留上限的旧输出分片。"""
        while self.retained_bytes > TERMINAL_MAX_RETAINED_BYTES and self.chunks:
            removed = self.chunks.pop(0)
            self.retained_bytes -= removed.byte_size
            self.retained_from_seq = removed.seq + 1

    def mark_finished(self, exit_code: Optional[int]) -> None:
        """标记进程已经结束，并记录退出码。"""
        self.exit_code = exit_code
        self.status = "killed" if self.kill_requested else "exited"
        if not self.use_pty:
            self.stdin_closed = True
        self.updated_at = time.time()
        self.notify_changed()

    def mark_error(self, message: str) -> None:
        """标记会话异常，保留错误信息供后续读取。"""
        self.error = message
        self.status = "error"
        self.updated_at = time.time()
        self.notify_changed()

    def close_pty(self) -> None:
        """关闭父进程持有的 PTY master fd。"""
        if self.master_fd is None:
            return
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.master_fd = None
