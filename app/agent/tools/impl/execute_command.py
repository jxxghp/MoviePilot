"""执行 Shell 命令工具。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Optional, TextIO, Type

from pydantic import BaseModel, Field

from app.agent.tools.impl._command_safety import validate_command_safety
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._terminal_session import (
    TERMINAL_DEFAULT_READ_BYTES,
    TERMINAL_MAX_READ_BYTES,
    TERMINAL_WAIT_DEFAULT_MS,
    terminal_session_manager,
)
from app.log import logger


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
        return text.encode("utf-8")[:byte_limit].decode("utf-8", errors="replace")

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

    action: Optional[Literal["start", "read", "wait", "write", "kill", "run"]] = Field(
        "start",
        description=(
            "Command action. start launches a managed background session and returns "
            "session_id. read/wait/write/kill operate on that session. run executes "
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
        description="Text to send to stdin for action=write. Use \\u0003 for Ctrl+C.",
    )
    signal_name: Optional[str] = Field(
        "TERM",
        description="Signal for action=kill, such as TERM, INT, KILL, or 15.",
    )
    cwd: Optional[str] = Field(
        None,
        description="Working directory for action=start or action=run.",
    )
    env: Optional[dict[str, Any]] = Field(
        None,
        description="Additional environment variables for action=start.",
    )
    use_pty: Optional[bool] = Field(
        True,
        description="Use a pseudo terminal for action=start when supported.",
    )
    since_seq: Optional[int] = Field(
        None,
        description="For action=read/wait, return output chunks after this seq.",
    )
    max_bytes: Optional[int] = Field(
        TERMINAL_DEFAULT_READ_BYTES,
        description="For action=read/wait, maximum output bytes to return.",
    )
    timeout_ms: Optional[int] = Field(
        TERMINAL_WAIT_DEFAULT_MS,
        description="For action=wait, maximum segmented wait time in milliseconds.",
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
        "launches a background session and immediately returns session_id/status/"
        "last_seq/output_until_seq. Call the same tool with action=read, wait, "
        "write, or kill to poll output, wait in short segments, send stdin, or "
        "terminate it. Use action=run only when a one-shot bounded command result "
        "is preferred."
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
            return f"写入命令输入: {session_id or ''}"
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
        """按块读取一次性命令输出，保留 32KB 头尾预览。"""
        while True:
            chunk = await stream.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            output.append(stream_name, chunk.decode("utf-8", errors="replace"))

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
    ) -> str:
        """格式化 action=run 的兼容文本结果。"""
        if timed_out:
            result = f"命令执行超时 (限制: {timeout}秒，已终止进程)"
        else:
            result = f"命令执行完成 (退出码: {exit_code})"

        if timeout_note:
            result += f"\n\n提示:\n{timeout_note}"
        if output.temp_file_path:
            file_note = "截至命令终止前的完整输出" if timed_out else "完整输出"
            result += (
                "\n\n提示:\n"
                f"命令输出超过 {MAX_OUTPUT_PREVIEW_BYTES // 1024}KB，"
                f"仅返回前后各 {MAX_OUTPUT_HEAD_BYTES // 1024}KB 预览。\n"
                f"{file_note}已写入临时文件: {output.temp_file_path}\n"
                "如需完整内容，请继续读取该文件。"
            )
        if output.combined_preview:
            result += f"\n\n命令输出预览:\n{output.combined_preview}"
        if output.preview_truncated:
            result += "\n\n...(仅展示前后各 16KB 内容)"
        if not output.combined_preview:
            result += "\n\n(无输出内容)"
        return result

    async def _run_once(
        self,
        *,
        command: str,
        timeout: Optional[int],
        cwd: Optional[str] = None,
        confirm_dangerous: bool = False,
    ) -> str:
        """按旧模式一次性执行命令，等待完成或超时后返回文本结果。"""
        self._validate_command(command, confirmed=confirm_dangerous)
        normalized_timeout, timeout_note = self._normalize_timeout(timeout)

        async with _command_semaphore:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                **self._subprocess_kwargs(),
            )
            output = _CommandOutput(preview_limit_bytes=MAX_OUTPUT_PREVIEW_BYTES)
            wait_task = asyncio.create_task(process.wait())
            reader_tasks = [
                asyncio.create_task(self._read_stream(process.stdout, "stdout", output)),
                asyncio.create_task(self._read_stream(process.stderr, "stderr", output)),
            ]

            timed_out = False
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task), timeout=normalized_timeout
                )
            except asyncio.TimeoutError:
                timed_out = True
                await self._cleanup_process(process, wait_task)
            except asyncio.CancelledError:
                await self._cleanup_process(process, wait_task)
                raise

            try:
                await self._finish_reader_tasks(reader_tasks)
            finally:
                output.close()

        return self._format_run_result(
            exit_code=process.returncode,
            output=output,
            timeout=normalized_timeout,
            timed_out=timed_out,
            timeout_note=timeout_note,
        )

    async def run(
        self,
        action: Optional[str] = "start",
        command: Optional[str] = None,
        session_id: Optional[str] = None,
        input_text: Optional[str] = None,
        signal_name: Optional[str] = "TERM",
        cwd: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        use_pty: Optional[bool] = True,
        since_seq: Optional[int] = None,
        max_bytes: Optional[int] = TERMINAL_DEFAULT_READ_BYTES,
        timeout_ms: Optional[int] = TERMINAL_WAIT_DEFAULT_MS,
        timeout: Optional[int] = 60,
        confirm_dangerous: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """执行命令动作：默认后台启动，也支持读取、等待、写入、终止和一次性执行。"""
        normalized_action = (action or "start").strip().lower()
        logger.info(
            f"执行工具: {self.name}, action={normalized_action}, "
            f"command={command}, session_id={session_id}"
        )

        try:
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
                    use_pty=use_pty,
                    confirm_dangerous=bool(confirm_dangerous),
                )
                return self._dump(payload)

            if normalized_action == "read":
                payload = await terminal_session_manager.read(
                    session_id=self._require_session_id(session_id),
                    since_seq=since_seq,
                    max_bytes=max_bytes,
                )
                return self._dump(payload)

            if normalized_action == "wait":
                payload = await terminal_session_manager.wait(
                    session_id=self._require_session_id(session_id),
                    timeout_ms=timeout_ms,
                    since_seq=since_seq,
                    max_bytes=max_bytes,
                )
                return self._dump(payload)

            if normalized_action == "write":
                payload = await terminal_session_manager.write(
                    session_id=self._require_session_id(session_id),
                    input_text=input_text or "",
                )
                return self._dump(payload)

            if normalized_action == "kill":
                payload = await terminal_session_manager.kill(
                    session_id=self._require_session_id(session_id),
                    sig=signal_name,
                )
                return self._dump(payload)

            if normalized_action == "run":
                return await self._run_once(
                    command=self._require_command(command),
                    timeout=timeout,
                    cwd=cwd,
                    confirm_dangerous=bool(confirm_dangerous),
                )

            raise ValueError(f"不支持的 action: {action}")
        except Exception as err:
            logger.error(f"执行命令 action 失败: {err}", exc_info=True)
            return self._dump({"error": str(err), "status": "error", "action": normalized_action})
