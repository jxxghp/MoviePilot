"""执行 Shell 命令工具。"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Optional, TextIO, Type

from pydantic import BaseModel, Field

from app.agent.shell import build_agent_subprocess_env, resolve_agent_cwd, resolve_agent_shell
from app.agent.terminal.manager import (
    TERMINAL_WAIT_DEFAULT_MS,
    TERMINAL_YIELD_DEFAULT_MS,
    get_terminal_session_manager,
)
from app.agent.terminal.output import TERMINAL_DEFAULT_READ_BYTES, TERMINAL_MAX_READ_BYTES, TerminalOutputError
from app.agent.terminal.ownership import TerminalAccessError, TerminalScope, require_terminal_scope
from app.agent.tools.base import DEFAULT_TOOL_RESULT_MAX_CHARS, MoviePilotTool
from app.agent.tools.impl._command_safety import validate_command_safety
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_PREVIEW_BYTES = 32 * 1024
MAX_OUTPUT_HEAD_BYTES = 16 * 1024
MAX_OUTPUT_TAIL_BYTES = 16 * 1024
READ_CHUNK_SIZE = 4096
KILL_GRACE_SECONDS = 3
COMMAND_CONCURRENCY_LIMIT = 2
_command_semaphore = asyncio.Semaphore(COMMAND_CONCURRENCY_LIMIT)


@dataclass
class _CommandOutput:
    """保存命令头尾预览，并在超限时将完整输出写入临时文件。"""

    preview_limit_bytes: int
    preview_entries: list[tuple[str, str]] = field(default_factory=list)
    tail_entries: deque[tuple[str, str]] = field(default_factory=deque)
    captured_bytes: int = 0
    tail_bytes: int = 0
    preview_truncated: bool = False
    temp_file_path: Optional[str] = None
    temp_file_handle: Optional[TextIO] = None
    last_written_stream: Optional[str] = None

    @staticmethod
    def _clip_text_to_bytes(text: str, byte_limit: int) -> str:
        """按 UTF-8 字节数截断文本，避免截断后出现非法字符。"""
        if byte_limit <= 0:
            return ""
        return text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")

    def _write_chunk(self, stream_name: str, text: str) -> None:
        """把输出分片按 stdout/stderr 分段写入临时文件。"""
        if not self.temp_file_handle or not text:
            return

        if self.last_written_stream != stream_name:
            if self.temp_file_handle.tell() > 0:
                self.temp_file_handle.write("\n")
            title = "标准输出" if stream_name == "stdout" else "错误输出"
            self.temp_file_handle.write(f"[{title}]\n")
            self.last_written_stream = stream_name

        self.temp_file_handle.write(text)

    def _ensure_temp_file(self) -> None:
        """首次超出预览上限时创建临时文件并补写已缓存预览。"""
        if self.temp_file_handle:
            return

        temp_file = NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".log",
            prefix="moviepilot-command-",
            delete=False,
        )
        self.temp_file_path = temp_file.name
        self.temp_file_handle = temp_file
        for stream_name, chunk in self.preview_entries:
            self._write_chunk(stream_name, chunk)

    def close(self) -> None:
        """关闭临时文件句柄，确保输出落盘。"""
        if not self.temp_file_handle:
            return
        self.temp_file_handle.flush()
        self.temp_file_handle.close()
        self.temp_file_handle = None

    def append(self, stream_name: str, text: str) -> None:
        """追加一段输出，超出预览上限后保留头尾预览和完整日志文件。"""
        if not text:
            return

        self._append_tail(stream_name, text)

        if self.temp_file_handle:
            self._write_chunk(stream_name, text)
            return

        chunk_bytes = len(text.encode("utf-8"))
        remaining = self.preview_limit_bytes - self.captured_bytes
        if chunk_bytes <= remaining:
            self.preview_entries.append((stream_name, text))
            self.captured_bytes += chunk_bytes
            return

        self.preview_truncated = True
        self._ensure_temp_file()
        self._write_chunk(stream_name, text)

        preview = self._clip_text_to_bytes(text, remaining)
        if preview:
            self.preview_entries.append((stream_name, preview))
            self.captured_bytes += len(preview.encode("utf-8"))

    def _append_tail(self, stream_name: str, text: str) -> None:
        """维护固定字节大小的尾部输出，方便定位测试和构建失败信息。"""
        self.tail_entries.append((stream_name, text))
        self.tail_bytes += len(text.encode("utf-8"))
        while self.tail_bytes > MAX_OUTPUT_TAIL_BYTES and self.tail_entries:
            old_stream, old_text = self.tail_entries.popleft()
            old_bytes = len(old_text.encode("utf-8"))
            overflow = self.tail_bytes - MAX_OUTPUT_TAIL_BYTES
            if old_bytes <= overflow:
                self.tail_bytes -= old_bytes
                continue
            kept_text = old_text.encode("utf-8")[overflow:].decode(
                "utf-8", errors="ignore"
            )
            kept_bytes = len(kept_text.encode("utf-8"))
            self.tail_bytes -= old_bytes
            if kept_text:
                self.tail_entries.appendleft((old_stream, kept_text))
                self.tail_bytes += kept_bytes

    @staticmethod
    def _format_entries(entries: list[tuple[str, str]]) -> str:
        """按 stdout/stderr 切换插入可读的输出分段标题。"""
        parts: list[str] = []
        last_stream: Optional[str] = None
        for stream_name, text in entries:
            if stream_name != last_stream:
                title = "标准输出" if stream_name == "stdout" else "错误输出"
                parts.append(f"\n[{title}]\n")
                last_stream = stream_name
            parts.append(text)
        return "".join(parts).strip()

    @property
    def combined_preview(self) -> str:
        """返回完整输出或头尾组合预览。"""
        if not self.preview_truncated:
            return self._format_entries(self.preview_entries)

        head_entries: list[tuple[str, str]] = []
        remaining = MAX_OUTPUT_HEAD_BYTES
        for stream_name, text in self.preview_entries:
            if remaining <= 0:
                break
            clipped = self._clip_text_to_bytes(text, remaining)
            if clipped:
                head_entries.append((stream_name, clipped))
                remaining -= len(clipped.encode("utf-8"))
        head = self._format_entries(head_entries)
        tail = self._format_entries(list(self.tail_entries))
        return (
            f"{head}\n\n...(中间输出已省略，完整内容在临时文件中)...\n\n{tail}"
        ).strip()

    @property
    def stdout(self) -> str:
        """返回当前保留的 stdout 预览。"""
        return "".join(
            text for stream_name, text in self.preview_entries if stream_name == "stdout"
        ).strip()

    @property
    def stderr(self) -> str:
        """返回当前保留的 stderr 预览。"""
        return "".join(
            text for stream_name, text in self.preview_entries if stream_name == "stderr"
        ).strip()


class ExecuteCommandInput(BaseModel):
    """执行 Shell 命令工具的输入参数模型。"""

    action: Optional[Literal["start", "read", "wait", "write", "interrupt", "kill", "run"]] = Field(
        "start",
        description=(
            "Command action. start launches a managed background session and returns "
            "session_id. read/wait/write/interrupt/kill operate on that session. interrupt sends one "
            "interrupt without escalating to a forced kill; kill terminates the session. run executes "
            "once and waits until completion or timeout."
        ),
    )
    command: Optional[str] = Field(
        None,
        description="Shell command. Required for action=start or action=run.",
    )
    session_id: Optional[str] = Field(
        None,
        description="Command session id returned by action=start.",
    )
    input_text: Optional[str] = Field(
        None,
        description=(
            "Text to send to stdin for action=write. In pipe mode, \\u0003/\\u0004 are ordinary bytes. "
            "In PTY mode their Ctrl+C/EOF behavior depends on terminal settings. Use action=interrupt for a signal."
        ),
    )
    close_stdin: Optional[bool] = Field(
        False, strict=True,
        description=(
            "For action=write in pipe mode, send optional final input then close stdin so the command receives EOF. "
            "Output stays readable. Empty input alone does not send EOF. PTY half-close is not supported."
        ),
    )
    signal_name: Optional[str] = Field(
        "TERM",
        description="Signal for action=kill, such as TERM, INT, KILL, or 15. Invalid or unsupported signals are rejected.",
    )
    cwd: Optional[str] = Field(
        None,
        description="Launch directory for start/run. Omitted or relative paths use the MoviePilot root directory; ~ is expanded.",
    )
    shell: Optional[str] = Field(
        None,
        description="Shell executable for start/run. All pipe and PTY launches use the same selection policy and report the selected shell.",
    )
    login: Optional[bool] = Field(
        None, strict=True,
        description=(
            "For start/run, explicitly enable or disable login-shell startup where supported. "
            "POSIX defaults to non-login; Windows preserves its configured shell defaults. "
            "Login startup files can change environment and directory."
        ),
    )
    env: Optional[dict[str, Any]] = Field(
        None,
        description="Additional environment variables for action=start or action=run.",
    )
    use_pty: Optional[bool] = Field(
        True,
        description="Use a pseudo terminal for action=start when supported.",
    )
    since_seq: Optional[int] = Field(
        None, ge=0, strict=True,
        description="For read/wait/write/interrupt/kill, last fully delivered output seq. Resume with output_until_seq, never last_seq.",
    )
    since_offset: Optional[int] = Field(
        None, ge=0, strict=True,
        description=(
            "UTF-8 byte offset within the chunk after since_seq. Pass 0 to enable partial-chunk paging; "
            "resume using output_until_seq and output_until_offset together. Omit for legacy whole-chunk reads."
        ),
    )
    max_bytes: Optional[int] = Field(
        TERMINAL_DEFAULT_READ_BYTES,
        description="For start/read/wait/write/interrupt/kill, maximum output bytes to return.",
    )
    timeout_ms: Optional[int] = Field(
        TERMINAL_WAIT_DEFAULT_MS,
        description="For action=wait, wait for unread output or output completion; 0 returns immediately without stopping the process.",
    )
    yield_time_ms: Optional[int] = Field(
        TERMINAL_YIELD_DEFAULT_MS, ge=0, strict=True,
        description="For action=start, first-output wait budget in milliseconds (default 250, capped at 10000); 0 returns immediately.",
    )
    timeout: Optional[int] = Field(
        60,
        description="For action=run, max execution time in seconds.",
    )
    confirm_dangerous: Optional[bool] = Field(
        False,
        description=(
            "Explicit confirmation for high-risk commands such as recursive root deletion, "
            "disk formatting, shutdown/reboot, or destructive permission changes."
        ),
    )


class ExecuteCommandTool(MoviePilotTool):
    """统一执行和管理 Shell 命令的 Agent 工具。"""

    name: str = "execute_command"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Command,
        ToolTag.Admin,
    ]
    description: str = (
        "Start and manage shell commands on the server. By default action=start "
        "launches a background session, waits briefly for initial output, then returns its session_id and output cursor. "
        "Continue with both output_until_seq and output_until_offset; last_seq is not a consumed cursor. "
        "Call the same tool with action=read, wait, "
        "write, interrupt, or kill to poll output, wait in short segments, send stdin, "
        "send one interrupt, or terminate it. write(close_stdin=true) sends EOF in pipe mode after optional final input; "
        "PTY input and output cannot be half-closed. start/run share cwd, shell and login selection. "
        "Use action=run only when a one-shot bounded command result "
        "is preferred. run returns JSON with exit_code, timed_out, execution_outcome, "
        "output preview and optional output_file. Only a normal zero exit is success; "
        "a timeout does not undo any side effects."
    )
    args_schema: Type[BaseModel] = ExecuteCommandInput
    require_admin: bool = True
    result_max_chars = TERMINAL_MAX_READ_BYTES + 4096

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据命令动作生成友好的提示消息。"""
        action = kwargs.get("action") or "start"
        command = kwargs.get("command")
        session_id = kwargs.get("session_id")
        if action in {"start", "run"}:
            return f"执行系统命令: {command or ''}"
        if action == "read":
            return f"读取命令输出: {session_id or ''}"
        if action == "wait":
            return f"等待命令会话: {session_id or ''}"
        if action == "write":
            if kwargs.get("close_stdin"):
                return f"关闭命令输入: {session_id or ''}"
            return f"写入命令输入: {session_id or ''}"
        if action == "interrupt":
            return f"中断命令会话: {session_id or ''}"
        if action == "kill":
            return f"终止命令会话: {session_id or ''}"
        return f"处理命令会话: {session_id or command or ''}"

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        """把结构化命令会话结果转换为 Agent 容易解析的 JSON 字符串。"""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _require_session_id(session_id: Optional[str]) -> str:
        """校验会话型 action 必须传入 session_id。"""
        if not session_id:
            raise ValueError("action 需要传入 session_id")
        return session_id

    @staticmethod
    def _require_command(command: Optional[str]) -> str:
        """校验启动型 action 必须传入 command。"""
        if not command or not command.strip():
            raise ValueError("action 需要传入 command")
        return command

    @staticmethod
    def _validate_command(command: str, *, confirmed: bool = False) -> None:
        """复用旧工具的基础危险命令过滤，避免明显破坏性命令进入 shell。"""
        validate_command_safety(command, confirmed=confirmed)

    @staticmethod
    def _normalize_timeout(timeout: Optional[int]) -> tuple[int, Optional[str]]:
        """限制一次性执行命令的最长运行时间。"""
        try:
            normalized = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            normalized = DEFAULT_TIMEOUT_SECONDS

        if normalized <= 0:
            return DEFAULT_TIMEOUT_SECONDS, "timeout 参数无效，已使用默认 60 秒"
        if normalized > MAX_TIMEOUT_SECONDS:
            return (
                MAX_TIMEOUT_SECONDS,
                f"timeout 参数超过上限，已从 {normalized} 秒限制为 {MAX_TIMEOUT_SECONDS} 秒",
            )
        return normalized, None

    @staticmethod
    def _subprocess_kwargs() -> dict:
        """为一次性命令创建独立进程组，便于超时清理整棵子进程。"""
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return kwargs

    @staticmethod
    async def _read_stream(
        stream: asyncio.StreamReader,
        stream_name: str,
        output: _CommandOutput,
    ) -> None:
        """每个流增量解码 UTF-8，跨读取边界的字符不能被提前替换。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(READ_CHUNK_SIZE)
            if not chunk:
                output.append(stream_name, decoder.decode(b"", final=True))
                break
            output.append(stream_name, decoder.decode(chunk))

    @staticmethod
    def _terminate_process(process: Any, sig: int) -> None:
        """向进程组发送终止信号，不支持进程组的平台回退为单进程终止。"""
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == getattr(signal, "SIGKILL", None):
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    @classmethod
    async def _cleanup_process(
        cls,
        process: Any,
        wait_task: asyncio.Task,
    ) -> None:
        """先温和终止，失败后强杀，避免超时 shell 遗留子进程。"""
        if wait_task.done():
            return

        cls._terminate_process(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task), timeout=KILL_GRACE_SECONDS
            )
            return
        except asyncio.TimeoutError:
            pass

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        cls._terminate_process(process, kill_signal)
        try:
            await asyncio.wait_for(
                asyncio.shield(wait_task), timeout=KILL_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(f"命令进程强制清理超时: pid={process.pid}")

    @staticmethod
    async def _finish_reader_tasks(reader_tasks: list[asyncio.Task]) -> None:
        """等待一次性命令输出读取任务退出，异常只记录不影响工具返回。"""
        if not reader_tasks:
            return
        done, pending = await asyncio.wait(reader_tasks, timeout=1)
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*done, *pending, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.debug(f"命令输出读取任务异常: {result}")

    @staticmethod
    def _format_run_result(
        *,
        exit_code: Optional[int],
        output: _CommandOutput,
        timeout: int,
        timed_out: bool,
        timeout_note: Optional[str],
        cwd: Optional[str] = None,
        shell: Optional[str] = None,
        login: bool = False,
        scope_cancelled: bool = False,
    ) -> str:
        """分开返回机器可判定的执行状态与有界输出，不能靠完成提示推断成功。"""
        if scope_cancelled:
            result = "命令因任务作用域关闭而取消，未确认业务动作是否完成"
        elif exit_code is None:
            result = "无法确认命令进程已结束，请先核对实际状态"
        elif timed_out:
            result = f"命令执行超时 (限制: {timeout}秒，已终止进程)"
        else:
            result = f"命令执行完成 (退出码: {exit_code})"

        if timeout_note:
            result += f"\n\n提示:\n{timeout_note}"
        if output.temp_file_path:
            file_note = "截至返回时已捕获的输出" if exit_code is None else ("截至命令终止前的完整输出" if timed_out else "完整输出")
            result += (
                "\n\n提示:\n"
                f"命令输出超过 {MAX_OUTPUT_PREVIEW_BYTES // 1024}KB，"
                f"仅返回前后各 {MAX_OUTPUT_HEAD_BYTES // 1024}KB 预览。\n"
                f"{file_note}已写入临时文件: {output.temp_file_path}\n"
                "如需完整内容，请继续读取该文件。"
            )
        if output.preview_truncated:
            result += "\n\n...(仅展示前后各 16KB 内容)"
        if not output.combined_preview:
            result += "\n\n(无输出内容)"
        succeeded = exit_code == 0 and not timed_out and not scope_cancelled
        outcome = "failed" if scope_cancelled else (
            "unknown" if exit_code is None else ("succeeded" if succeeded else "failed")
        )
        return ExecuteCommandTool._dump({
            "action": "run", "success": succeeded, "execution_outcome": outcome,
            "status": "cancelled" if scope_cancelled else (
                "unknown" if exit_code is None else ("timed_out" if timed_out else "exited")
            ),
            "exit_code": exit_code, "timed_out": timed_out, "timeout": timeout,
            "cwd": cwd, "shell": shell, "login": login, "stdin_closed": True,
            "output_truncated": output.preview_truncated, "output_file": output.temp_file_path,
            "output": output.combined_preview, "message": result,
        })

    async def _run_once(
        self,
        *,
        command: str,
        timeout: Optional[int],
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        shell: Optional[str] = None,
        login: Optional[bool] = None,
        confirm_dangerous: bool = False,
    ) -> str:
        """一次性执行命令并返回结构化终态；退出路径都必须释放读取任务和归档句柄。"""
        self._validate_command(command, confirmed=confirm_dangerous)
        scope = require_terminal_scope()
        scope.begin_run()
        try:
            return await self._run_once_with_scope(
                scope=scope, command=command, timeout=timeout, cwd=cwd, env=env,
                shell=shell, login=login, confirm_dangerous=confirm_dangerous,
            )
        finally:
            scope.finish_run()

    @staticmethod
    async def _acquire_command_slot(scope: TerminalScope) -> None:
        """并发槽等待期间响应作用域封口，禁止取消后迟到启动一次性进程。"""
        acquire_task = asyncio.create_task(_command_semaphore.acquire())
        closed_task = asyncio.create_task(scope.changed.wait())
        acquired = False
        released = False
        try:
            done, _ = await asyncio.wait(
                {acquire_task, closed_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if closed_task in done and acquire_task not in done:
                acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
                raise TerminalAccessError()
            await acquire_task
            acquired = True
            if scope.closed:
                _command_semaphore.release()
                released = True
                acquired = False
                raise TerminalAccessError()
        finally:
            if not acquire_task.done():
                acquire_task.cancel()
            await asyncio.gather(acquire_task, return_exceptions=True)
            if acquire_task.done() and not acquire_task.cancelled() and not acquired and not released:
                _command_semaphore.release()
            if not closed_task.done():
                closed_task.cancel()
            await asyncio.gather(closed_task, return_exceptions=True)

    async def _run_once_with_scope(
        self,
        *,
        scope: TerminalScope,
        command: str,
        timeout: Optional[int],
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        shell: Optional[str] = None,
        login: Optional[bool] = None,
        confirm_dangerous: bool = False,
    ) -> str:
        """在已登记作用域下运行一次命令，并对封口和进程收尾保持可观察。"""
        normalized_timeout, timeout_note = self._normalize_timeout(timeout)
        normalized_cwd = resolve_agent_cwd(cwd, root_path=get_runtime_setting("ROOT_PATH"))
        normalized_env = build_agent_subprocess_env(env)
        shell_policy = resolve_agent_shell(executable=shell, login=login, environment=normalized_env, cwd=normalized_cwd)

        await self._acquire_command_slot(scope)
        try:
            require_terminal_scope()
            process = await asyncio.create_subprocess_exec(
                *shell_policy.build_argv(command), cwd=normalized_cwd, env=normalized_env,
                **self._subprocess_kwargs(),
            )
            output = _CommandOutput(preview_limit_bytes=MAX_OUTPUT_PREVIEW_BYTES)
            wait_task = asyncio.create_task(process.wait())
            reader_tasks = [
                asyncio.create_task(self._read_stream(process.stdout, "stdout", output)),
                asyncio.create_task(self._read_stream(process.stderr, "stderr", output)),
            ]

            timed_out = False
            scope_cancelled = False
            scope_task = asyncio.create_task(scope.changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {wait_task, scope_task}, timeout=normalized_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                scope_cancelled = scope_task in done
                if wait_task not in done:
                    timed_out = not scope_cancelled
                    await self._cleanup_process(process, wait_task)
            except asyncio.CancelledError:
                await self._cleanup_process(process, wait_task)
                raise

            finally:
                if not scope_task.done():
                    scope_task.cancel()
                await asyncio.gather(scope_task, return_exceptions=True)
                try:
                    await self._finish_reader_tasks(reader_tasks)
                finally:
                    output.close()

            return self._format_run_result(
                exit_code=process.returncode,
                output=output,
                timeout=normalized_timeout,
                timed_out=timed_out,
                scope_cancelled=scope_cancelled,
                timeout_note=timeout_note,
                cwd=normalized_cwd, shell=shell_policy.executable, login=shell_policy.login,
            )
        finally:
            _command_semaphore.release()

    async def run(
        self,
        action: Optional[str] = "start",
        command: Optional[str] = None,
        session_id: Optional[str] = None,
        input_text: Optional[str] = None,
        close_stdin: Optional[bool] = False,
        signal_name: Optional[str] = "TERM",
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        shell: Optional[str] = None,
        login: Optional[bool] = None,
        use_pty: Optional[bool] = True,
        since_seq: Optional[int] = None,
        since_offset: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        timeout_ms: Optional[int] = TERMINAL_WAIT_DEFAULT_MS,
        yield_time_ms: Optional[int] = TERMINAL_YIELD_DEFAULT_MS,
        timeout: Optional[int] = 60,
        confirm_dangerous: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """按同一启动策略执行命令，区分写入/EOF、一次中断与会话终止。"""
        normalized_action = (action or "start").strip().lower()
        logger.info(
            f"执行工具: {self.name}, action={normalized_action}, "
            f"command={command}, session_id={session_id}"
        )

        try:
            require_terminal_scope()
            terminal_session_manager = get_terminal_session_manager()
            output_budget = DEFAULT_TOOL_RESULT_MAX_CHARS
            if self.result_max_chars and self.result_max_chars > 0:
                output_budget = min(self.result_max_chars, output_budget)
            if normalized_action == "start":
                start_command = self._require_command(command)
                self._validate_command(
                    start_command,
                    confirmed=bool(confirm_dangerous),
                )
                payload = await terminal_session_manager.start(
                    command=start_command,
                    cwd=cwd,
                    env=env,
                    shell=shell,
                    login=login,
                    use_pty=use_pty,
                    confirm_dangerous=bool(confirm_dangerous),
                    yield_time_ms=yield_time_ms,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "read":
                payload = await terminal_session_manager.read(
                    session_id=self._require_session_id(session_id),
                    since_seq=since_seq,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "wait":
                payload = await terminal_session_manager.wait(
                    session_id=self._require_session_id(session_id),
                    timeout_ms=timeout_ms,
                    since_seq=since_seq,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "write":
                payload = await terminal_session_manager.write(
                    session_id=self._require_session_id(session_id),
                    input_text=input_text or "",
                    close_stdin=False if close_stdin is None else close_stdin,
                    since_seq=since_seq,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "interrupt":
                payload = await terminal_session_manager.interrupt(
                    session_id=self._require_session_id(session_id),
                    since_seq=since_seq,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "kill":
                payload = await terminal_session_manager.kill(
                    session_id=self._require_session_id(session_id),
                    sig=signal_name,
                    since_seq=since_seq,
                    since_offset=since_offset,
                    max_bytes=max_bytes,
                    max_output_chars=output_budget,
                )
                return self._dump(payload)

            if normalized_action == "run":
                return await self._run_once(
                    command=self._require_command(command),
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                    shell=shell,
                    login=login,
                    confirm_dangerous=bool(confirm_dangerous),
                )

            raise ValueError(f"不支持的 action: {action}")
        except TerminalAccessError as err:
            return self._dump({
                "error": str(err), "status": "error", "action": normalized_action,
                "success": False, "execution_outcome": "failed", "code": "terminal_access_denied",
            })
        except TerminalOutputError as err:
            return self._dump({
                "error": str(err), "status": "error", "action": normalized_action,
                "success": False, "execution_outcome": "failed",
                "code": err.code, "minimum_read_bytes": err.minimum_read_bytes,
            })
        except Exception as err:
            logger.error(f"执行命令 action 失败: {err}", exc_info=True)
            return self._dump({"error": str(err), "status": "error", "action": normalized_action,
                               "success": False, "execution_outcome": "failed"})
