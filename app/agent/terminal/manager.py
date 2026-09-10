"""Agent 终端会话管理器。"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import time
import uuid
from types import ModuleType
from typing import Any, Optional

from app.agent.shell import AgentShell, build_agent_subprocess_env, resolve_agent_cwd, resolve_agent_shell
from app.agent.terminal.output import (
    TERMINAL_DEFAULT_READ_BYTES,
    TerminalOutputError,
    read_payload,
    resolve_cursor,
    validate_output_budget,
)
from app.agent.terminal.ownership import TerminalAccessError, TerminalScope, require_terminal_scope
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
TERMINAL_READ_CHUNK_SIZE = 4096
TERMINAL_PTY_POLL_INTERVAL = 0.05
TERMINAL_WAIT_DEFAULT_MS = 1000
TERMINAL_WAIT_MAX_MS = 60 * 1000
TERMINAL_YIELD_DEFAULT_MS = 250
TERMINAL_YIELD_MAX_MS = 10 * 1000
TERMINAL_KILL_GRACE_SECONDS = 3
_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
_SHARE_ACTIONS = frozenset({"read", "wait", "write", "interrupt", "kill"})

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
        self._owner_starts: dict[TerminalScope, int] = {}
        self._owner_idle: dict[TerminalScope, asyncio.Event] = {}
        self._grants: dict[TerminalScope, dict[str, frozenset[str]]] = {}

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
        owner = require_terminal_scope()
        self._validate_command(command, confirmed=confirm_dangerous)
        validate_output_budget(max_output_chars)
        if since_offset is not None and (type(since_offset) is not int or since_offset != 0):
            raise TerminalOutputError("新会话的 since_offset 只能为 0 或 null")
        initial_wait = self._normalize_yield_timeout(yield_time_ms)
        normalized_cwd = resolve_agent_cwd(cwd, root_path=get_runtime_setting('ROOT_PATH'))
        normalized_env = build_agent_subprocess_env(env)
        shell_policy = resolve_agent_shell(executable=shell, login=login, environment=normalized_env, cwd=normalized_cwd)
        should_use_pty = self._normalize_bool(use_pty, default=True) and os.name == "posix"

        async with self._lock:
            if owner.closed:
                raise TerminalAccessError()
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
            self._owner_starts[owner] = self._owner_starts.get(owner, 0) + 1
            self._owner_idle.setdefault(owner, asyncio.Event()).clear()

        session: Optional[_TerminalSession] = None
        slot_released = False
        launch = asyncio.create_task(
            self._start_pty_session(command, normalized_cwd, normalized_env, shell_policy=shell_policy)
            if should_use_pty else self._start_pipe_session(
                command, normalized_cwd, normalized_env, shell_policy=shell_policy,
            )
        )
        try:
            session = await asyncio.shield(launch)
            session.owner = owner
            async with self._lock:
                if self._closed:
                    raise RuntimeError("终端会话管理器已关闭")
                if owner.closed:
                    raise TerminalAccessError()
                self._sessions[session.session_id] = session
                self._release_start_locked(owner)
                slot_released = True
            self._check_access(session, owner, "start")
            logger.info(
                f"启动后台终端会话: session_id={session.session_id}, pid={session.pid}, "
                f"use_pty={session.use_pty}, command={command}"
            )
            payload = await self._wait_for_output(
                session, timeout_ms=initial_wait, since_seq=0, since_offset=since_offset,
                max_bytes=max_bytes, preserve_output_error=True, max_output_chars=max_output_chars,
                extra_fields={"yield_time_ms": initial_wait}, scope=owner, action="start",
            )
            self._check_access(session, owner, "start")
            return payload
        except BaseException:
            cleanup_task = asyncio.create_task(self._recover_started_session(launch, session, owner))
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    # 再次取消调用者也不能把启动预留与未交付的真实进程分离。
                    if cleanup_task.done():
                        break
                    continue
            raise
        finally:
            if not slot_released:
                async with self._lock:
                    self._release_start_locked(owner)

    async def _recover_started_session(
        self, launch: asyncio.Task[_TerminalSession], session: Optional[_TerminalSession], owner: TerminalScope,
    ) -> None:
        """启动预留持有迟到进程；先回收再取得登记锁，避免取消被登记阻塞。"""
        if session is None:
            try:
                session = await launch
            except Exception:
                return
            session.owner = owner
        await self._discard_started_session(session)

    def _release_start_locked(self, owner: TerminalScope) -> None:
        """同一登记锁中释放全局与任务启动预留，关闭者才可确认快照完整。"""
        self._starting -= 1
        if self._starting == 0:
            self._starts_idle.set()
        remaining = self._owner_starts[owner] - 1
        if remaining:
            self._owner_starts[owner] = remaining
        else:
            self._owner_starts.pop(owner)
            self._owner_idle.pop(owner).set()

    async def _discard_started_session(self, session: _TerminalSession) -> None:
        """首次返回前取消时回收无从寻址的进程，并撤销其会话登记。"""
        converged = await self._terminate_session(session)
        async with self._lock:
            if converged and self._sessions.get(session.session_id) is session:
                self._sessions.pop(session.session_id)
            elif not converged:
                self._sessions[session.session_id] = session

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
        session, _ = self._get_accessible_session(session_id, "read")
        return read_payload(
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
        session, scope = self._get_accessible_session(session_id, "wait")
        normalized_timeout = self._normalize_wait_timeout(timeout_ms)
        payload = await self._wait_for_output(
            session, timeout_ms=normalized_timeout, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes,
            max_output_chars=max_output_chars, extra_fields={"wait_timeout_ms": normalized_timeout},
            scope=scope, action="wait",
        )
        return payload

    async def _wait_for_output(
        self, session: _TerminalSession, *, timeout_ms: int, since_seq: Optional[int] = None,
        since_offset: Optional[int] = None, max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        preserve_output_error: bool = False, max_output_chars: Optional[int] = None,
        extra_fields: Optional[dict[str, Any]] = None,
        scope: TerminalScope, action: str,
    ) -> dict[str, Any]:
        """先捕获通知再查输出，无数据到 await 之间的变化也能唤醒所有等待者。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            self._check_access(session, scope, action)
            changed = session.changed_event
            payload = read_payload(
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
            await self._wait_for_change(session, scope, changed, remaining)

    @staticmethod
    async def _wait_for_change(
        session: _TerminalSession, scope: TerminalScope, changed: asyncio.Event, timeout: float,
    ) -> None:
        """输出和双方作用域封口任一变化都结束等待，取消时收齐短命通知任务。"""
        events = {changed, scope.changed}
        if session.owner is not None:
            events.add(session.owner.changed)
        waiters = [asyncio.create_task(event.wait()) for event in events]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def write(
        self, *, session_id: str, input_text: str, since_seq: Optional[int] = None,
        since_offset: Optional[int] = None, max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        max_output_chars: Optional[int] = None, close_stdin: bool = False,
    ) -> dict[str, Any]:
        """串行写入输入和可选管道 EOF；关闭 stdin 不影响输出读取器。"""
        session, scope = self._get_accessible_session(session_id, "write")
        validate_output_budget(max_output_chars)
        resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        if type(close_stdin) is not bool:
            raise ValueError("close_stdin 必须为布尔值")
        if close_stdin and session.use_pty:
            raise ValueError("PTY 不支持独立关闭 stdin；输出端保持打开，请使用程序自己的结束命令")
        data = (input_text or "").encode("utf-8")
        written = 0
        async with session.input_lock:
            self._check_access(session, scope, "write")
            if session.stdin_closed:
                if data or not close_stdin:
                    raise RuntimeError("会话 stdin 已关闭，不能再写入输入")
            elif session.status != "running":
                raise RuntimeError(f"会话已结束，当前状态: {session.status}")
            elif session.use_pty:
                while written < len(data):
                    self._check_access(session, scope, "write")
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
                await self._write_pipe_input(session, scope, data, close_stdin=close_stdin)
                written = len(data)

        self._check_access(session, scope, "write")
        session.updated_at = time.time()
        payload = read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, preserve_output_error=True,
            max_output_chars=max_output_chars, extra_fields={"written_bytes": written},
        )
        return payload

    async def _write_pipe_input(
        self, session: _TerminalSession, scope: TerminalScope, data: bytes, *, close_stdin: bool,
    ) -> None:
        """在会话输入锁内先排空末段，再半关闭并等待写端收尾。"""
        writer = session.process.stdin if session.process else None
        if writer is None:
            raise RuntimeError("进程 stdin 不可写")
        if data:
            writer.write(data)
            await writer.drain()
            self._check_access(session, scope, "write")
        if close_stdin:
            self._check_access(session, scope, "write")
            writer.close()
            session.stdin_closed = True
            await writer.wait_closed()

    async def interrupt(
        self, *, session_id: str, since_seq: Optional[int] = None, since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES, max_output_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """只发送一次真实中断信号，不改变终止意图，也不等待或升级成强杀。"""
        session, _ = self._get_accessible_session(session_id, "interrupt")
        validate_output_budget(max_output_chars)
        resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
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
        return read_payload(
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
        session, scope = self._get_accessible_session(session_id, "kill")
        validate_output_budget(max_output_chars)
        resolve_cursor(session, since_seq=since_seq, since_offset=since_offset)
        signal_number = self._resolve_signal(sig)
        if session.status == "running":
            session.kill_requested = True
            self._send_signal(session, signal_number)
            if not await self._wait_for_exit(session):
                self._check_access(session, scope, "kill")
                self._send_signal(session, _KILL_SIGNAL)

        self._check_access(session, scope, "kill")
        return read_payload(
            session, since_seq=since_seq, since_offset=since_offset, max_bytes=max_bytes, preserve_output_error=True,
            max_output_chars=max_output_chars,
        )

    async def close(self) -> None:
        """停止全部会话，未确认进程和读取器收尾的记录保留给下次关闭。"""
        async with self._close_lock:
            async with self._lock:
                self._closed = True
                for owner in {*self._owner_starts, *(session.owner for session in self._sessions.values())}:
                    if owner is not None:
                        owner.seal()
                for child in self._grants:
                    child.seal()
                self._grants.clear()

            await self._starts_idle.wait()

            async with self._lock:
                sessions = list(self._sessions.values())

            results = await asyncio.gather(
                *(self._terminate_session(session) for session in sessions),
                return_exceptions=True,
            )

            async with self._lock:
                for session, converged in zip(sessions, results):
                    if converged is True:
                        self._sessions.pop(session.session_id, None)

    async def close_owner(self, owner: TerminalScope) -> bool:
        """先封口并撤销能力，再只收敛本任务的启动和进程；未收敛时保留事实供重试。"""
        owner.seal()
        async with self._lock:
            revoked = set(self._grants.pop(owner, {}))
            owned = {key for key, session in self._sessions.items() if session.owner is owner}
            for grants in self._grants.values():
                for key in owned:
                    grants.pop(key, None)
            for key in revoked | owned:
                if key in self._sessions:
                    self._sessions[key].notify_changed()
            starting = self._owner_idle.get(owner)
        if starting is not None:
            try:
                await asyncio.wait_for(starting.wait(), timeout=TERMINAL_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                return False
        async with self._lock:
            sessions = [session for session in self._sessions.values() if session.owner is owner]
        results = await asyncio.gather(
            *(self._terminate_session(session) for session in sessions), return_exceptions=True,
        )
        async with self._lock:
            for session, converged in zip(sessions, results):
                if converged is True:
                    self._sessions.pop(session.session_id, None)
            return not self._owner_starts.get(owner) and not any(
                session.owner is owner for session in self._sessions.values()
            )

    async def _terminate_session(self, session: _TerminalSession) -> bool:
        """同一终端的关闭串行执行，真实收尾前不丢弃进程或输出读取器。"""
        async with session.termination_lock:
            if session.status == "running":
                session.kill_requested = True
                self._send_signal(session, signal.SIGTERM)
            if not await self._wait_for_exit(session):
                if session.status == "running":
                    self._send_signal(session, _KILL_SIGNAL)
                if not await self._wait_for_exit(session):
                    logger.error(f"终端会话关闭超时: session_id={session.session_id}, pid={session.pid}")
                    return False
            if any(not task.done() for task in session.reader_tasks):
                return False
            session.finish_output()
            session.close_pty()
            return True

    @staticmethod
    async def _wait_for_exit(session: _TerminalSession) -> bool:
        """复用终止与关闭的有界等待，超时不取消会话原本的进程收尾任务。"""
        if session.wait_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(session.wait_task), timeout=TERMINAL_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                return False
            except asyncio.CancelledError:
                if not session.wait_task.cancelled():
                    raise
                return False
            except Exception:
                return False
        return session.status in {"exited", "killed"} and (
            session.process is None or session.process.returncode is not None
        )

    def _get_accessible_session(self, session_id: str, action: str) -> tuple[_TerminalSession, TerminalScope]:
        """所有用户动作共用归属查表，未知句柄与无权访问返回同一最小错误。"""
        scope = require_terminal_scope()
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalAccessError()
        self._check_access(session, scope, action)
        return session, scope

    def _check_access(self, session: _TerminalSession, scope: TerminalScope, action: str) -> None:
        """每个可暂停边界重新核对原调用者，封口或撤销后不再泄露输出或继续写入。"""
        if (
            self._closed or scope.closed or not scope.user_id or not scope.task_id
            or session.owner is None or session.owner.closed
            or self._sessions.get(session.session_id) is not session
            or (session.owner is not scope and action not in self._grants.get(scope, {}).get(
                session.session_id, frozenset(),
            ))
        ):
            raise TerminalAccessError()

    def share(self, parent: TerminalScope, child: TerminalScope, grants: dict[str, frozenset[str]]) -> None:
        """宿主原子授予同用户指定任务的有限动作；受授者不能继续转授。"""
        if (
            require_terminal_scope() is not parent or parent is child or child.closed
            or not child.task_id or not child.user_id or parent.user_id != child.user_id
        ):
            raise TerminalAccessError()
        for key, actions in grants.items():
            session, _ = self._get_accessible_session(key, "read")
            if session.owner is not parent or not isinstance(actions, frozenset) or not actions <= _SHARE_ACTIONS:
                raise TerminalAccessError()
        self._grants.setdefault(child, {}).update(grants)

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




terminal_session_manager = _TerminalSessionManager()


def get_terminal_session_manager() -> _TerminalSessionManager:
    """返回当前进程的终端会话管理器，避免复用已完成关停的实例。"""
    global terminal_session_manager
    if (
        terminal_session_manager._closed and not terminal_session_manager._sessions
        and terminal_session_manager._starting == 0
    ):
        terminal_session_manager = _TerminalSessionManager()
    return terminal_session_manager
