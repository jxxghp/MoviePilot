"""Agent 终端会话管理器。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import subprocess
import time
import uuid
from types import ModuleType
from typing import Any, Optional

from app.agent.shell import AgentShell, build_agent_subprocess_env, resolve_agent_cwd, resolve_agent_shell
from app.agent.terminal.session import TERMINAL_RETENTION_SECONDS, _TerminalSession
from app.agent.tools.impl._command_safety import validate_command_safety
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

if os.name == "posix":
    import fcntl as _posix_fcntl
    import pty as _posix_pty

    _fcntl: Optional[ModuleType] = _posix_fcntl
    _pty: Optional[ModuleType] = _posix_pty
else:
    _fcntl = None
    _pty = None


TERMINAL_CONCURRENCY_LIMIT = 4
TERMINAL_DEFAULT_READ_BYTES = 10 * 1024
TERMINAL_MAX_READ_BYTES = 64 * 1024
TERMINAL_READ_CHUNK_SIZE = 4096
TERMINAL_PTY_POLL_INTERVAL = 0.05
TERMINAL_WAIT_DEFAULT_MS = 1000
TERMINAL_WAIT_MAX_MS = 60 * 1000
TERMINAL_YIELD_DEFAULT_MS = 250
TERMINAL_YIELD_MAX_MS = 10 * 1000
TERMINAL_KILL_GRACE_SECONDS = 3
_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


class TerminalOutputError(ValueError):
    """携带稳定错误码和最小页预算的可恢复输出读取错误。"""

    def __init__(self, message: str, *, code: str = "invalid_output_cursor", minimum_read_bytes: Optional[int] = None) -> None:
        """保留结构化恢复提示，调用方不必解析中文消息。"""
        super().__init__(message)
        self.code = code
        self.minimum_read_bytes = minimum_read_bytes


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
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
        return kwargs

    async def start(
        self,
        *,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        use_pty: Any = True,
        confirm_dangerous: bool = False,
        yield_time_ms: Optional[int] = TERMINAL_YIELD_DEFAULT_MS,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        since_offset: Optional[int] = None,
        max_output_chars: Optional[int] = None,
        shell: Optional[str] = None,
        login: Optional[bool] = None,
    ) -> dict[str, Any]:
        """启动后台命令，在首个输出、完成或首次等待预算到期时交付会话。"""
        self._validate_command(command, confirmed=confirm_dangerous)
        self._validate_output_budget(max_output_chars)
        if since_offset is not None and (type(since_offset) is not int or since_offset != 0):
            raise TerminalOutputError("新会话的 since_offset 只能为 0 或 null")
        initial_wait = self._normalize_yield_timeout(yield_time_ms)
        normalized_cwd = resolve_agent_cwd(cwd, root_path=get_runtime_setting('ROOT_PATH'))
        normalized_env = build_agent_subprocess_env(env)
        shell_policy = resolve_agent_shell(executable=shell, login=login, environment=normalized_env, cwd=normalized_cwd)
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
        slot_released = False
        session_released = False
        try:
            session = (
                await self._start_pty_session(command, normalized_cwd, normalized_env, shell_policy=shell_policy)
                if should_use_pty
                else await self._start_pipe_session(
                    command, normalized_cwd, normalized_env, shell_policy=shell_policy
                )
            )

            async with self._lock:
                reject_session = self._closed
                if not reject_session:
                    self._sessions[session.session_id] = session
                    self._starting -= 1
                    slot_released = True
                    if self._starting == 0:
                        self._starts_idle.set()

            if reject_session:
                await self._terminate_session(session)
                session_released = True
                raise RuntimeError("终端会话管理器已关闭")
            logger.info(
                f"启动后台终端会话: session_id={session.session_id}, pid={session.pid}, "
                f"use_pty={session.use_pty}, command={command}"
            )
            payload = await self._wait_for_output(
                session, timeout_ms=initial_wait, since_seq=0, since_offset=since_offset,
                max_bytes=max_bytes, preserve_output_error=True, max_output_chars=max_output_chars,
                extra_fields={"yield_time_ms": initial_wait},
            )
            if self._closed:
                raise RuntimeError("终端会话管理器已关闭")
            return payload
        except BaseException:
            if session is not None and not session_released:
                cleanup_task = asyncio.create_task(self._discard_started_session(session))
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
            raise
        finally:
            if not slot_released:
                async with self._lock:
                    self._starting -= 1
                    if self._starting == 0:
                        self._starts_idle.set()

    async def _discard_started_session(self, session: _TerminalSession) -> None:
        """首次返回前取消时回收无从寻址的进程，并撤销其会话登记。"""
        await self._terminate_session(session)
        async with self._lock:
            if self._sessions.get(session.session_id) is session:
                self._sessions.pop(session.session_id)

    async def _start_pty_session(
        self, command: str, cwd: str, env: dict[str, str], *, shell_policy: Optional[AgentShell] = None,
    ) -> _TerminalSession:
        """通过 PTY fork 启动交互式命令会话。"""
        if _pty is None:
            raise RuntimeError("当前平台不支持 PTY 会话")
        shell_policy = shell_policy or resolve_agent_shell(environment=env, cwd=cwd)
        argv = shell_policy.build_argv(command)
        pid, master_fd = _pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                # exec 失败的子进程不能回到继承的事件循环或执行父进程清理逻辑。
                os._exit(127)

        self._set_nonblocking(master_fd)
        session = _TerminalSession(
            session_id=f"term_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=cwd,
            pid=pid,
            use_pty=True,
            master_fd=master_fd,
            shell_policy=shell_policy,
        )
        session.reader_tasks.append(asyncio.create_task(self._read_pty(session)))
        session.wait_task = asyncio.create_task(self._wait_pty_process(session))
        return session

    async def _start_pipe_session(
        self, command: str, cwd: str, env: dict[str, str], *, shell_policy: Optional[AgentShell] = None,
    ) -> _TerminalSession:
        """通过普通 stdin/stdout/stderr 管道启动命令会话。"""
        shell_policy = shell_policy or resolve_agent_shell(environment=env, cwd=cwd)
        process = await asyncio.create_subprocess_exec(
            *shell_policy.build_argv(command), cwd=cwd, env=env, **self._pipe_subprocess_kwargs(),
        )
        session = _TerminalSession(
            session_id=f"term_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=cwd,
            pid=process.pid or 0,
            use_pty=False,
            process=process,
            shell_policy=shell_policy,
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
        complete = False
        try:
            while session.master_fd is not None:
                try:
                    data = os.read(session.master_fd, TERMINAL_READ_CHUNK_SIZE)
                except BlockingIOError:
                    await asyncio.sleep(TERMINAL_PTY_POLL_INTERVAL)
                    continue
                except OSError as err:
                    complete = err.errno == errno.EIO
                    if err.errno not in {errno.EIO, errno.EBADF}:
                        logger.debug(f"PTY 输出读取异常: session_id={session.session_id}, error={err}")
                    break
                if not data:
                    complete = True
                    break
                session.append_output("pty", data)
        finally:
            session.finish_stream("pty", complete=complete)

    @staticmethod
    async def _read_pipe(
        session: _TerminalSession,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> None:
        """持续从普通管道读取增量输出。"""
        complete = False
        try:
            while True:
                data = await stream.read(TERMINAL_READ_CHUNK_SIZE)
                if not data:
                    complete = True
                    break
                session.append_output(stream_name, data)
        finally:
            session.finish_stream(stream_name, complete=complete)

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
        if session.reader_tasks:
            done, pending = await asyncio.wait(session.reader_tasks, timeout=1)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*done, *pending, return_exceptions=True)
            if pending or any(isinstance(result, BaseException) for result in results):
                session.output_lost = True
        session.finish_output()

    async def read(
        self,
        *,
        session_id: str,
        since_seq: Optional[int] = None,
        since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        max_output_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """读取会话当前保留的增量输出。"""
        session = self.get_session(session_id)
        return self._read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, max_output_chars=max_output_chars,
        )

    async def wait(
        self,
        *,
        session_id: str,
        timeout_ms: Optional[int] = TERMINAL_WAIT_DEFAULT_MS,
        since_seq: Optional[int] = None,
        since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        max_output_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """等待未读输出或输出最终收尾；零预算只取快照，不终止后台命令。"""
        session = self.get_session(session_id)
        normalized_timeout = self._normalize_wait_timeout(timeout_ms)
        payload = await self._wait_for_output(
            session, timeout_ms=normalized_timeout, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes,
            max_output_chars=max_output_chars, extra_fields={"wait_timeout_ms": normalized_timeout},
        )
        return payload

    async def _wait_for_output(
        self, session: _TerminalSession, *, timeout_ms: int, since_seq: Optional[int] = None,
        since_offset: Optional[int] = None, max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        preserve_output_error: bool = False, max_output_chars: Optional[int] = None,
        extra_fields: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """先捕获通知再查输出，无数据到 await 之间的变化也能唤醒所有等待者。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            changed = session.changed_event
            payload = self._read_payload(
                session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes,
                preserve_output_error=preserve_output_error, max_output_chars=max_output_chars,
                extra_fields={**(extra_fields or {}), "wait_reason": "completed"},
            )
            # 捕获丢失是持久事实；只有本次请求落在已淘汰位置才是新的可读缺口。
            new_gap = since_seq is not None and since_seq < session.retained_from_seq - 1
            if payload["output"] or new_gap or payload.get("output_error"):
                payload["wait_reason"] = "output"
                return payload
            if session.output_complete:
                payload["wait_reason"] = "completed"
                return payload
            remaining = deadline - loop.time()
            if remaining <= 0:
                payload["wait_reason"] = "timeout"
                return payload
            try:
                await asyncio.wait_for(changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # 到期时再取一次一致快照，避免刚到达的数据只体现在高水位里。
                pass

    async def write(
        self, *, session_id: str, input_text: str, since_seq: Optional[int] = None,
        since_offset: Optional[int] = None, max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        max_output_chars: Optional[int] = None, close_stdin: bool = False,
    ) -> dict[str, Any]:
        """串行写入输入和可选管道 EOF；关闭 stdin 不影响输出读取器。"""
        session = self.get_session(session_id)
        self._validate_output_budget(max_output_chars)
        self._resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        if type(close_stdin) is not bool:
            raise ValueError("close_stdin 必须为布尔值")
        if close_stdin and session.use_pty:
            raise ValueError("PTY 不支持独立关闭 stdin；输出端保持打开，请使用程序自己的结束命令")
        data = (input_text or "").encode("utf-8")
        written = 0
        async with session.input_lock:
            if session.stdin_closed:
                if data or not close_stdin:
                    raise RuntimeError("会话 stdin 已关闭，不能再写入输入")
            elif session.status != "running":
                raise RuntimeError(f"会话已结束，当前状态: {session.status}")
            elif session.use_pty:
                while written < len(data):
                    if session.master_fd is None:
                        raise RuntimeError("PTY 已关闭")
                    try:
                        # master 已设为非阻塞；同一事件循环完成写入，避免线程持有关闭前的 fd。
                        count = os.write(session.master_fd, memoryview(data)[written:])
                    except BlockingIOError:
                        await asyncio.sleep(TERMINAL_PTY_POLL_INTERVAL)
                        continue
                    if count <= 0:
                        raise BrokenPipeError("PTY 未接受后续输入")
                    written += count
            else:
                await self._write_pipe_input(session, data, close_stdin=close_stdin)
                written = len(data)

        session.updated_at = time.time()
        payload = self._read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, preserve_output_error=True,
            max_output_chars=max_output_chars, extra_fields={"written_bytes": written},
        )
        return payload

    @staticmethod
    async def _write_pipe_input(session: _TerminalSession, data: bytes, *, close_stdin: bool) -> None:
        """在会话输入锁内先排空末段，再半关闭并等待写端收尾。"""
        writer = session.process.stdin if session.process else None
        if writer is None:
            raise RuntimeError("进程 stdin 不可写")
        if data:
            writer.write(data)
            await writer.drain()
        if close_stdin:
            writer.close()
            session.stdin_closed = True
            await writer.wait_closed()

    async def interrupt(
        self, *, session_id: str, since_seq: Optional[int] = None, since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES, max_output_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """只发送一次真实中断信号，不改变终止意图，也不等待或升级成强杀。"""
        session = self.get_session(session_id)
        self._validate_output_budget(max_output_chars)
        self._resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        event = getattr(signal, "CTRL_BREAK_EVENT", None)
        sender = getattr(session.process, "send_signal", None)
        if os.name != "posix" and (event is None or not callable(sender)):
            raise NotImplementedError("当前平台不支持可独立发送的控制台中断，未终止进程")
        sent = False
        if session.status == "running":
            try:
                if os.name == "posix":
                    os.killpg(session.pid, signal.SIGINT)
                elif callable(sender):
                    sender(event)
                sent = True
            except ProcessLookupError:
                pass
        return self._read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, preserve_output_error=True,
            max_output_chars=max_output_chars,
            extra_fields={"signal": "SIGINT" if os.name == "posix" else "CTRL_BREAK_EVENT", "signal_sent": sent},
        )

    async def kill(
        self,
        *,
        session_id: str,
        sig: Optional[str | int] = "TERM",
        since_seq: Optional[int] = None,
        since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        max_output_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """向会话进程组发送信号并等待短暂清理。"""
        session = self.get_session(session_id)
        self._validate_output_budget(max_output_chars)
        self._resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        signal_number = self._resolve_signal(sig)
        if session.status == "running":
            session.kill_requested = True
            self._send_signal(session, signal_number)
            if not await self._wait_for_exit(session):
                self._send_signal(session, _KILL_SIGNAL)

        return self._read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, preserve_output_error=True,
            max_output_chars=max_output_chars,
        )

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

        if not await self._wait_for_exit(session):
            self._send_signal(session, _KILL_SIGNAL)
            if not await self._wait_for_exit(session):
                logger.error(f"终端会话关闭超时: session_id={session.session_id}, pid={session.pid}")

        for task in session.reader_tasks:
            if not task.done():
                task.cancel()
        if session.reader_tasks:
            await asyncio.gather(*session.reader_tasks, return_exceptions=True)
        session.finish_output()
        session.close_pty()

    @staticmethod
    async def _wait_for_exit(session: _TerminalSession) -> bool:
        """复用终止与关闭的有界等待，超时不取消会话原本的进程收尾任务。"""
        if session.wait_task is None or session.wait_task.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(session.wait_task), timeout=TERMINAL_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            return False
        return True

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
            normalized = int(TERMINAL_WAIT_DEFAULT_MS if timeout_ms is None else timeout_ms)
        except (TypeError, ValueError):
            normalized = TERMINAL_WAIT_DEFAULT_MS
        if normalized < 0:
            return 0
        return min(normalized, TERMINAL_WAIT_MAX_MS)

    @staticmethod
    def _normalize_yield_timeout(yield_time_ms: Optional[int]) -> int:
        """首次等待接受显式零值，直调入口也拒绝布尔和非法负数。"""
        if yield_time_ms is None:
            return TERMINAL_YIELD_DEFAULT_MS
        if type(yield_time_ms) is not int or yield_time_ms < 0:
            raise ValueError("yield_time_ms 必须为非负整数")
        return min(yield_time_ms, TERMINAL_YIELD_MAX_MS)

    @staticmethod
    def _validate_output_budget(max_output_chars: Optional[int]) -> None:
        """宿主内部预算必须容纳有界命令预览和完整恢复元数据。"""
        if max_output_chars is not None and (type(max_output_chars) is not int or max_output_chars < 4096):
            raise ValueError("max_output_chars 必须为空或至少 4096 的整数")

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

    @staticmethod
    def _resolve_cursor(
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

    @staticmethod
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

    def _collect_output(
        self,
        session: _TerminalSession,
        *,
        since_seq: Optional[int],
        since_offset: Optional[int] = None,
        max_bytes: Optional[int],
    ) -> dict[str, Any]:
        """按完整分片序号及下一分片字节偏移返回实际交付的输出页。"""
        read_limit = self._normalize_read_limit(max_bytes)
        seq, offset, lost = self._resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        selected_chunks = [chunk for chunk in session.chunks if chunk.seq > seq]
        output_parts: list[str] = []
        output_bytes = 0
        for chunk in selected_chunks:
            encoded = chunk.text.encode("utf-8")[offset:]
            remaining = read_limit - output_bytes
            if remaining == 0:
                break
            try:
                piece = self._slice_output(encoded, remaining, partial=since_offset is not None)
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

    def _read_payload(
        self, session: _TerminalSession, *, since_seq: Optional[int], since_offset: Optional[int],
        max_bytes: Optional[int], preserve_output_error: bool = False,
        max_output_chars: Optional[int] = None, extra_fields: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """动作已发生时保留会话句柄和未消费游标，只把分页错误作为附加恢复信息。"""
        self._validate_output_budget(max_output_chars)
        try:
            page = self._collect_output(session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes)
        except TerminalOutputError as error:
            if not preserve_output_error or error.code != "read_limit_too_small":
                raise
            seq, offset, lost = self._resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
            page = {
                "output": "", "output_until_seq": seq, "output_until_offset": offset,
                "output_truncated": True, "output_lost": lost,
                "output_error": {
                    "code": error.code, "message": str(error), "minimum_read_bytes": error.minimum_read_bytes,
                },
            }
        payload = {**self._session_payload(session, page), **(extra_fields or {})}
        if max_output_chars is None or len(json.dumps(payload, ensure_ascii=False, indent=2)) <= max_output_chars:
            return payload
        low, high = 1, self._normalize_read_limit(max_bytes) - 1
        best = None
        while low <= high:
            middle = (low + high) // 2
            try:
                candidate = self._read_payload(
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
        payload = self._read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=1,
            preserve_output_error=True, extra_fields=extra_fields,
        )
        payload["output_error"]["message"] = str(budget_error)
        return payload

    @staticmethod
    def _resolve_signal(sig: Optional[str | int]) -> int:
        """只接受已知有效且当前终止路径可真实发送的信号，绝不把拼写错误当 TERM。"""
        if isinstance(sig, bool) or (sig is not None and not isinstance(sig, (str, int))):
            raise ValueError("signal 必须为已知名称或有效正整数")
        number: Optional[int] = int(sig) if isinstance(sig, int) else None
        if not isinstance(sig, int):
            name = "TERM" if sig is None else sig.strip().upper()
            label = name if name.startswith("SIG") else f"SIG{name}"
            number = int(name) if name.isdigit() else signal.Signals.__members__.get(label)
            if label == "SIGKILL" and os.name == "nt":
                number = _KILL_SIGNAL
        valid = {int(value) for value in signal.valid_signals()}
        if os.name == "nt":
            valid = {int(signal.SIGTERM), int(_KILL_SIGNAL)}
        if not isinstance(number, int) or number <= 0 or number not in valid:
            raise ValueError("未知、无效或当前平台不支持的终止信号；非终止中断请使用 interrupt")
        return int(number)

    @staticmethod
    def _send_signal(session: _TerminalSession, sig: int) -> None:
        """优先向进程组发信号，失败时回退到单进程。"""
        try:
            if os.name == "posix":
                os.killpg(session.pid, sig)
            elif session.process:
                if sig == _KILL_SIGNAL:
                    session.process.kill()
                elif sig == signal.SIGTERM:
                    session.process.terminate()
                else:
                    raise ValueError("当前平台没有该信号的真实发送映射")
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

    @staticmethod
    def _session_payload(
        session: _TerminalSession,
        page: dict[str, Any],
    ) -> dict[str, Any]:
        """生成工具返回的结构化会话状态。"""
        command = _TerminalSessionManager._text_preview(session.command)
        cwd = _TerminalSessionManager._text_preview(session.cwd)
        error = _TerminalSessionManager._text_preview(session.error, 512) if session.error else session.error
        shell = session.shell_policy.executable if session.shell_policy else None
        shell_preview = _TerminalSessionManager._text_preview(shell, 256) if shell else shell
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


terminal_session_manager = _TerminalSessionManager()


def get_terminal_session_manager() -> _TerminalSessionManager:
    """返回当前进程的终端会话管理器，避免复用已完成关停的实例。"""
    global terminal_session_manager
    if terminal_session_manager._closed:
        terminal_session_manager = _TerminalSessionManager()
    return terminal_session_manager
