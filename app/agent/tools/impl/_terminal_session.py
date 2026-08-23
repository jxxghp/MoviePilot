"""Agent 终端会话管理器。"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.tools.impl._command_safety import validate_command_safety
from app.runtime.config import settings
from app.runtime.log import logger

if os.name == "posix":
    import fcntl as _fcntl
    import pty as _pty
else:
    _fcntl = None
    _pty = None


TERMINAL_CONCURRENCY_LIMIT = 4
TERMINAL_RETENTION_SECONDS = 30 * 60
TERMINAL_MAX_RETAINED_BYTES = 1024 * 1024
TERMINAL_DEFAULT_READ_BYTES = 10 * 1024
TERMINAL_MAX_READ_BYTES = 64 * 1024
TERMINAL_READ_CHUNK_SIZE = 4096
TERMINAL_PTY_POLL_INTERVAL = 0.05
TERMINAL_WAIT_DEFAULT_MS = 1000
TERMINAL_WAIT_MAX_MS = 60 * 1000
TERMINAL_KILL_GRACE_SECONDS = 3


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
    reader_tasks: list[asyncio.Task] = field(default_factory=list)
    wait_task: Optional[asyncio.Task] = None

    def append_output(self, stream: str, data: bytes) -> None:
        """追加输出并按容量上限丢弃最旧分片，避免长任务撑爆内存。"""
        if not data:
            return

        text = data.decode("utf-8", errors="replace")
        chunk = _TerminalChunk(
            seq=self.next_seq,
            stream=stream,
            text=text,
            byte_size=len(data),
            created_at=time.time(),
        )
        self.next_seq += 1
        self.chunks.append(chunk)
        self.retained_bytes += chunk.byte_size
        self.updated_at = chunk.created_at
        self._trim_output()

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
        self.updated_at = time.time()

    def mark_error(self, message: str) -> None:
        """标记会话异常，保留错误信息供后续读取。"""
        self.error = message
        self.status = "error"
        self.updated_at = time.time()

    def close_pty(self) -> None:
        """关闭父进程持有的 PTY master fd。"""
        if self.master_fd is None:
            return
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.master_fd = None


class _TerminalSessionManager:
    """管理 Agent 后台终端会话的生命周期。"""

    def __init__(self) -> None:
        """初始化会话表和并发保护锁。"""
        self._sessions: dict[str, _TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._starting = 0
        self._starts_idle = asyncio.Event()
        self._starts_idle.set()

    @staticmethod
    def _normalize_bool(value: Any, default: bool = True) -> bool:
        """兼容 LLM 或 HTTP 传入的 bool/string/int 布尔值。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)

    @staticmethod
    def _normalize_cwd(cwd: Optional[str]) -> str:
        """解析工作目录，未传入时默认使用 MoviePilot 项目根目录。"""
        if not cwd:
            return str(settings.ROOT_PATH)
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            path = (settings.ROOT_PATH / path).resolve()
        else:
            path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"工作目录不存在: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"工作目录不是目录: {path}")
        return str(path)

    @staticmethod
    def _build_env(env: Optional[dict[str, Any]]) -> dict[str, str]:
        """合并环境变量，并把值稳定转换为字符串。"""
        merged_env = os.environ.copy()
        if not env:
            return merged_env
        for key, value in env.items():
            if value is None:
                continue
            merged_env[str(key)] = str(value)
        return merged_env

    @staticmethod
    def _validate_command(command: str, *, confirmed: bool = False) -> None:
        """拒绝明显危险或空白命令。"""
        validate_command_safety(command, confirmed=confirmed)

    @staticmethod
    def _set_nonblocking(fd: int) -> None:
        """将 PTY master fd 设置为非阻塞，避免后台读取任务卡住事件循环。"""
        if _fcntl is None:
            raise RuntimeError("当前平台不支持 PTY 非阻塞设置")
        flags = _fcntl.fcntl(fd, _fcntl.F_GETFL)
        _fcntl.fcntl(fd, _fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def _pipe_subprocess_kwargs() -> dict[str, Any]:
        """生成普通管道模式的子进程参数。"""
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return kwargs

    async def start(
        self,
        *,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        use_pty: Any = True,
        confirm_dangerous: bool = False,
    ) -> dict[str, Any]:
        """启动后台命令并立即返回会话 ID。"""
        self._validate_command(command, confirmed=confirm_dangerous)
        normalized_cwd = self._normalize_cwd(cwd)
        normalized_env = self._build_env(env)
        should_use_pty = self._normalize_bool(use_pty, default=True) and os.name == "posix"

        async with self._lock:
            if self._closed:
                raise RuntimeError("终端会话管理器已关闭")
            self._cleanup_finished_sessions_locked()
            if (
                    self._active_session_count_locked() + self._starting
                    >= TERMINAL_CONCURRENCY_LIMIT
            ):
                raise RuntimeError(
                    f"后台终端会话数已达到上限 {TERMINAL_CONCURRENCY_LIMIT}"
                )
            self._starting += 1
            self._starts_idle.clear()

        session: Optional[_TerminalSession] = None
        reject_session = False
        session_registered = False
        session_released = False
        try:
            session = (
                await self._start_pty_session(command, normalized_cwd, normalized_env)
                if should_use_pty
                else await self._start_pipe_session(
                    command, normalized_cwd, normalized_env
                )
            )

            async with self._lock:
                reject_session = self._closed
                if not reject_session:
                    self._sessions[session.session_id] = session
                    session_registered = True

            if reject_session:
                await self._terminate_session(session)
                session_released = True
                raise RuntimeError("终端会话管理器已关闭")
        except BaseException:
            if session is not None and not session_registered and not session_released:
                cleanup_task = asyncio.create_task(self._terminate_session(session))
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
            raise
        finally:
            async with self._lock:
                self._starting -= 1
                if self._starting == 0:
                    self._starts_idle.set()

        logger.info(
            "启动后台终端会话: session_id=%s, pid=%s, use_pty=%s, command=%s",
            session.session_id,
            session.pid,
            session.use_pty,
            command,
        )
        await asyncio.sleep(0)
        return self._session_payload(session, output="", output_truncated=False)

    async def _start_pty_session(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> _TerminalSession:
        """通过 PTY fork 启动交互式命令会话。"""
        if _pty is None:
            raise RuntimeError("当前平台不支持 PTY 会话")
        pid, master_fd = _pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(env)
            shell = os.environ.get("SHELL") or "/bin/sh"
            os.execl(shell, shell, "-lc", command)

        self._set_nonblocking(master_fd)
        session = _TerminalSession(
            session_id=f"term_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=cwd,
            pid=pid,
            use_pty=True,
            master_fd=master_fd,
        )
        session.reader_tasks.append(asyncio.create_task(self._read_pty(session)))
        session.wait_task = asyncio.create_task(self._wait_pty_process(session))
        return session

    async def _start_pipe_session(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> _TerminalSession:
        """通过普通 stdin/stdout/stderr 管道启动命令会话。"""
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=env,
            **self._pipe_subprocess_kwargs(),
        )
        session = _TerminalSession(
            session_id=f"term_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=cwd,
            pid=process.pid or 0,
            use_pty=False,
            process=process,
        )
        if process.stdout:
            session.reader_tasks.append(
                asyncio.create_task(self._read_pipe(session, process.stdout, "stdout"))
            )
        if process.stderr:
            session.reader_tasks.append(
                asyncio.create_task(self._read_pipe(session, process.stderr, "stderr"))
            )
        session.wait_task = asyncio.create_task(self._wait_pipe_process(session))
        return session

    @staticmethod
    async def _read_pty(session: _TerminalSession) -> None:
        """持续从 PTY 读取增量输出。"""
        while session.master_fd is not None:
            try:
                data = os.read(session.master_fd, TERMINAL_READ_CHUNK_SIZE)
            except BlockingIOError:
                await asyncio.sleep(TERMINAL_PTY_POLL_INTERVAL)
                continue
            except OSError as err:
                if err.errno not in {errno.EIO, errno.EBADF}:
                    logger.debug(
                        f"PTY 输出读取异常: session_id={session.session_id}, "
                        f"error={err}"
                    )
                break

            if not data:
                break
            session.append_output("pty", data)

    @staticmethod
    async def _read_pipe(
            session: _TerminalSession,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> None:
        """持续从普通管道读取增量输出。"""
        while True:
            data = await stream.read(TERMINAL_READ_CHUNK_SIZE)
            if not data:
                break
            session.append_output(stream_name, data)

    async def _wait_pty_process(self, session: _TerminalSession) -> None:
        """等待 PTY 子进程结束并完成输出读取任务收尾。"""
        try:
            _, status = await asyncio.to_thread(os.waitpid, session.pid, 0)
            exit_code = os.waitstatus_to_exitcode(status)
            session.mark_finished(exit_code)
        except ChildProcessError:
            session.mark_finished(session.exit_code)
        except Exception as err:
            session.mark_error(str(err))
            logger.warning(
                f"等待 PTY 进程失败: session_id={session.session_id}, error={err}"
            )
        finally:
            await self._finish_reader_tasks(session)
            session.close_pty()

    async def _wait_pipe_process(self, session: _TerminalSession) -> None:
        """等待普通管道子进程结束并完成输出读取任务收尾。"""
        try:
            if not session.process:
                session.mark_error("进程对象不存在")
                return
            exit_code = await session.process.wait()
            session.mark_finished(exit_code)
        except Exception as err:
            session.mark_error(str(err))
            logger.warning(
                f"等待管道进程失败: session_id={session.session_id}, error={err}"
            )
        finally:
            await self._finish_reader_tasks(session)

    @staticmethod
    async def _finish_reader_tasks(session: _TerminalSession) -> None:
        """等待输出读取任务退出，超时后取消残留任务。"""
        if not session.reader_tasks:
            return
        done, pending = await asyncio.wait(session.reader_tasks, timeout=1)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

    async def read(
        self,
        *,
        session_id: str,
        since_seq: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        """读取会话当前保留的增量输出。"""
        session = self.get_session(session_id)
        output, output_truncated, output_until_seq = self._collect_output(
            session,
            since_seq=since_seq,
            max_bytes=max_bytes,
        )
        return self._session_payload(
            session,
            output=output,
            output_truncated=output_truncated,
            output_until_seq=output_until_seq,
        )

    async def wait(
        self,
        *,
        session_id: str,
        timeout_ms: Optional[int] = TERMINAL_WAIT_DEFAULT_MS,
        since_seq: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        """短暂等待会话结束，并返回等待期间可见的增量输出。"""
        session = self.get_session(session_id)
        normalized_timeout = self._normalize_wait_timeout(timeout_ms)
        if session.wait_task and not session.wait_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(session.wait_task),
                    timeout=normalized_timeout / 1000,
                )
            except asyncio.TimeoutError:
                pass

        output, output_truncated, output_until_seq = self._collect_output(
            session,
            since_seq=since_seq,
            max_bytes=max_bytes,
        )
        payload = self._session_payload(
            session,
            output=output,
            output_truncated=output_truncated,
            output_until_seq=output_until_seq,
        )
        payload["wait_timeout_ms"] = normalized_timeout
        return payload

    async def write(self, *, session_id: str, input_text: str) -> dict[str, Any]:
        """向会话 stdin 写入文本，PTY 模式下写入 master fd。"""
        session = self.get_session(session_id)
        if session.status != "running":
            raise RuntimeError(f"会话已结束，当前状态: {session.status}")

        data = (input_text or "").encode("utf-8")
        if session.use_pty:
            if session.master_fd is None:
                raise RuntimeError("PTY 已关闭")
            await asyncio.to_thread(os.write, session.master_fd, data)
        else:
            if not session.process or not session.process.stdin:
                raise RuntimeError("进程 stdin 不可写")
            session.process.stdin.write(data)
            await session.process.stdin.drain()

        session.updated_at = time.time()
        payload = self._session_payload(session, output="", output_truncated=False)
        payload["written_bytes"] = len(data)
        return payload

    async def kill(
        self,
        *,
        session_id: str,
        sig: Optional[str | int] = "TERM",
    ) -> dict[str, Any]:
        """向会话进程组发送信号并等待短暂清理。"""
        session = self.get_session(session_id)
        if session.status != "running":
            return self._session_payload(session, output="", output_truncated=False)

        session.kill_requested = True
        signal_number = self._resolve_signal(sig)
        self._send_signal(session, signal_number)

        if session.wait_task and not session.wait_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(session.wait_task),
                    timeout=TERMINAL_KILL_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                force_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                self._send_signal(session, force_signal)

        return self._session_payload(session, output="", output_truncated=False)

    async def close(self) -> None:
        """停止所有后台终端会话并释放 PTY、读取任务和会话记录。"""
        async with self._close_lock:
            async with self._lock:
                self._closed = True

            await self._starts_idle.wait()

            async with self._lock:
                sessions = list(self._sessions.values())

            await asyncio.gather(
                *(self._terminate_session(session) for session in sessions),
                return_exceptions=True,
            )

            async with self._lock:
                for session in sessions:
                    session.close_pty()
                self._sessions.clear()

    async def _terminate_session(self, session: _TerminalSession) -> None:
        """以有限等待停止进程，并在必要时升级为 SIGKILL。"""
        if session.status == "running":
            session.kill_requested = True
            self._send_signal(session, signal.SIGTERM)

        wait_task = session.wait_task
        if wait_task and not wait_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=TERMINAL_KILL_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                force_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                self._send_signal(session, force_signal)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_task),
                        timeout=TERMINAL_KILL_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "终端会话关闭超时: session_id=%s, pid=%s",
                        session.session_id,
                        session.pid,
                    )

        for task in session.reader_tasks:
            if not task.done():
                task.cancel()
        if session.reader_tasks:
            await asyncio.gather(*session.reader_tasks, return_exceptions=True)
        session.close_pty()

    def get_session(self, session_id: str) -> _TerminalSession:
        """按 ID 获取会话，不存在时抛出清晰错误。"""
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"终端会话不存在: {session_id}")
        return session

    @staticmethod
    def _normalize_wait_timeout(timeout_ms: Optional[int]) -> int:
        """限制 wait 单次等待时间，避免工具调用长时间占用模型回合。"""
        try:
            normalized = int(timeout_ms or TERMINAL_WAIT_DEFAULT_MS)
        except (TypeError, ValueError):
            normalized = TERMINAL_WAIT_DEFAULT_MS
        if normalized < 0:
            return 0
        return min(normalized, TERMINAL_WAIT_MAX_MS)

    @staticmethod
    def _normalize_read_limit(max_bytes: Optional[int]) -> int:
        """限制单次读取返回的输出大小。"""
        try:
            normalized = int(max_bytes or TERMINAL_DEFAULT_READ_BYTES)
        except (TypeError, ValueError):
            normalized = TERMINAL_DEFAULT_READ_BYTES
        if normalized <= 0:
            return TERMINAL_DEFAULT_READ_BYTES
        return min(normalized, TERMINAL_MAX_READ_BYTES)

    def _collect_output(
        self,
        session: _TerminalSession,
        *,
        since_seq: Optional[int],
        max_bytes: Optional[int],
    ) -> tuple[str, bool, int]:
        """按 seq 和大小限制收集输出文本。"""
        read_limit = self._normalize_read_limit(max_bytes)
        selected_chunks = [
            chunk
            for chunk in session.chunks
            if since_seq is None or chunk.seq > since_seq
        ]
        output_parts: list[str] = []
        output_bytes = 0
        output_truncated = False
        last_stream: Optional[str] = None
        output_until_seq = since_seq or session.retained_from_seq - 1

        for chunk in selected_chunks:
            prefix = self._stream_prefix(chunk.stream, last_stream, session.use_pty)
            text = f"{prefix}{chunk.text}" if prefix else chunk.text
            encoded = text.encode("utf-8")
            remaining = read_limit - output_bytes
            if len(encoded) > remaining:
                if remaining > 0:
                    output_parts.append(
                        encoded[:remaining].decode("utf-8", errors="replace")
                    )
                output_truncated = True
                break
            output_parts.append(text)
            output_bytes += len(encoded)
            last_stream = chunk.stream
            output_until_seq = chunk.seq

        if since_seq is not None and since_seq < session.retained_from_seq - 1:
            output_truncated = True
        if not output_truncated:
            output_until_seq = session.next_seq - 1
        return "".join(output_parts), output_truncated, output_until_seq

    @staticmethod
    def _stream_prefix(stream: str, last_stream: Optional[str], use_pty: bool) -> str:
        """为普通管道输出增加 stdout/stderr 分段标识。"""
        if use_pty or stream == last_stream:
            return ""
        title = "标准输出" if stream == "stdout" else "错误输出"
        return f"\n[{title}]\n"

    @staticmethod
    def _resolve_signal(sig: Optional[str | int]) -> int:
        """解析字符串或数字形式的信号名。"""
        if isinstance(sig, int):
            return sig
        signal_name = str(sig or "TERM").strip().upper()
        if signal_name.isdigit():
            return int(signal_name)
        if not signal_name.startswith("SIG"):
            signal_name = f"SIG{signal_name}"
        return int(getattr(signal, signal_name, signal.SIGTERM))

    @staticmethod
    def _send_signal(session: _TerminalSession, sig: int) -> None:
        """优先向进程组发信号，失败时回退到单进程。"""
        try:
            if os.name == "posix":
                os.killpg(session.pid, sig)
            elif session.process:
                if sig == getattr(signal, "SIGKILL", None):
                    session.process.kill()
                else:
                    session.process.terminate()
        except ProcessLookupError:
            pass

    def _active_session_count_locked(self) -> int:
        """统计仍在运行的会话数量。"""
        return sum(1 for session in self._sessions.values() if session.status == "running")

    def _cleanup_finished_sessions_locked(self) -> None:
        """清理已经结束且超过保留时间的会话。"""
        now = time.time()
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.status != "running"
            and now - session.updated_at > TERMINAL_RETENTION_SECONDS
        ]
        for session_id in expired_ids:
            session = self._sessions.pop(session_id)
            session.close_pty()

    @staticmethod
    def _session_payload(
        session: _TerminalSession,
        *,
        output: str,
        output_truncated: bool,
        output_until_seq: Optional[int] = None,
    ) -> dict[str, Any]:
        """生成工具返回的结构化会话状态。"""
        return {
            "session_id": session.session_id,
            "command": session.command,
            "cwd": session.cwd,
            "pid": session.pid,
            "status": session.status,
            "exit_code": session.exit_code,
            "use_pty": session.use_pty,
            "last_seq": session.next_seq - 1,
            "output_until_seq": (
                session.next_seq - 1 if output_until_seq is None else output_until_seq
            ),
            "retained_from_seq": session.retained_from_seq,
            "output_truncated": output_truncated,
            "output": output,
            "error": session.error,
        }


terminal_session_manager = _TerminalSessionManager()


def get_terminal_session_manager() -> _TerminalSessionManager:
    """返回当前进程的终端会话管理器，避免复用已完成关停的实例。"""
    global terminal_session_manager
    if terminal_session_manager._closed:
        terminal_session_manager = _TerminalSessionManager()
    return terminal_session_manager
