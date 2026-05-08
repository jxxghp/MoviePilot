"""执行Shell命令工具"""

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass, field
from tempfile import NamedTemporaryFile
from typing import Optional, TextIO, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.log import logger


DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_PREVIEW_BYTES = 10 * 1024
READ_CHUNK_SIZE = 4096
KILL_GRACE_SECONDS = 3
COMMAND_CONCURRENCY_LIMIT = 2

_command_semaphore = asyncio.Semaphore(COMMAND_CONCURRENCY_LIMIT)


@dataclass
class _CommandOutput:
    """保存前 10KB 预览，并在超限时将完整输出写入临时文件。"""

    preview_limit_bytes: int
    preview_entries: list[tuple[str, str]] = field(default_factory=list)
    captured_bytes: int = 0
    preview_truncated: bool = False
    temp_file_path: Optional[str] = None
    temp_file_handle: Optional[TextIO] = None
    last_written_stream: Optional[str] = None

    @staticmethod
    def _clip_text_to_bytes(text: str, byte_limit: int) -> str:
        if byte_limit <= 0:
            return ""
        return text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")

    def _write_chunk(self, stream_name: str, text: str) -> None:
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
        if not self.temp_file_handle:
            return
        self.temp_file_handle.flush()
        self.temp_file_handle.close()
        self.temp_file_handle = None

    def append(self, stream_name: str, text: str) -> None:
        if not text:
            return

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

    @property
    def stdout(self) -> str:
        return "".join(
            text for stream_name, text in self.preview_entries if stream_name == "stdout"
        ).strip()

    @property
    def stderr(self) -> str:
        return "".join(
            text for stream_name, text in self.preview_entries if stream_name == "stderr"
        ).strip()


class ExecuteCommandInput(BaseModel):
    """执行Shell命令工具的输入参数模型"""

    explanation: str = Field(
        ..., description="Clear explanation of why this command is being executed"
    )
    command: str = Field(..., description="The shell command to execute")
    timeout: Optional[int] = Field(
        60, description="Max execution time in seconds (default: 60)"
    )


class ExecuteCommandTool(MoviePilotTool):
    name: str = "execute_command"
    description: str = (
        "Safely execute shell commands on the server. Useful for system "
        "maintenance, checking status, or running custom scripts. Includes "
        "timeout, concurrency, and output preview limits."
    )
    args_schema: Type[BaseModel] = ExecuteCommandInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据命令生成友好的提示消息"""
        command = kwargs.get("command", "")
        return f"执行系统命令: {command}"

    @staticmethod
    def _normalize_timeout(timeout: Optional[int]) -> tuple[int, Optional[str]]:
        """限制命令最长运行时间，避免 Agent 传入过大的 timeout。"""
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
        """为子进程创建独立进程组，便于超时场景清理整棵子进程。"""
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
        """按块读取输出，始终只把前 10KB 保留在返回结果中。"""
        while True:
            chunk = await stream.read(READ_CHUNK_SIZE)
            if not chunk:
                break

            output.append(stream_name, chunk.decode("utf-8", errors="replace"))

    @staticmethod
    def _terminate_process(process: asyncio.subprocess.Process, sig: int):
        """向进程组发送终止信号；不支持进程组的平台回退为单进程终止。"""
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
        process: asyncio.subprocess.Process,
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
            logger.warning("命令进程强制清理超时: pid=%s", process.pid)

    @staticmethod
    async def _finish_reader_tasks(reader_tasks: list[asyncio.Task]) -> None:
        """等待输出读取任务退出，异常只记录不影响工具返回。"""
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
                logger.debug("命令输出读取任务异常: %s", result)

    @staticmethod
    def _format_result(
        *,
        exit_code: Optional[int],
        output: _CommandOutput,
        timeout: int,
        timed_out: bool,
        timeout_note: Optional[str],
    ) -> str:
        if timed_out:
            result = f"命令执行超时 (限制: {timeout}秒，已终止进程)"
        else:
            result = f"命令执行完成 (退出码: {exit_code})"

        if timeout_note:
            result += f"\n\n提示:\n{timeout_note}"
        if output.temp_file_path:
            file_note = (
                "截至命令终止前的完整输出"
                if timed_out
                else "完整输出"
            )
            result += (
                "\n\n提示:\n"
                f"命令输出超过 10KB，仅返回前 {MAX_OUTPUT_PREVIEW_BYTES} 字节内容。\n"
                f"{file_note}已写入临时文件: {output.temp_file_path}\n"
                "如需完整内容，请继续读取该文件。"
            )
        if output.stdout:
            result += f"\n\n标准输出:\n{output.stdout}"
        if output.stderr:
            result += f"\n\n错误输出:\n{output.stderr}"
        if output.preview_truncated:
            result += "\n\n...(仅展示前 10KB 内容)"
        if not output.stdout and not output.stderr:
            result += "\n\n(无输出内容)"
        return result

    async def run(self, command: str, timeout: Optional[int] = 60, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: command={command}, timeout={timeout}"
        )

        # 简单安全过滤
        forbidden_keywords = [
            "rm -rf /",
            ":(){ :|:& };:",
            "dd if=/dev/zero",
            "mkfs",
            "reboot",
            "shutdown",
        ]
        for keyword in forbidden_keywords:
            if keyword in command:
                return f"错误：命令包含禁止使用的关键字 '{keyword}'"

        normalized_timeout, timeout_note = self._normalize_timeout(timeout)

        try:
            async with _command_semaphore:
                # 命令输出可能非常大，必须边读边落盘，不能使用 communicate() 一次性收集。
                process = await asyncio.create_subprocess_shell(
                    command, **self._subprocess_kwargs()
                )
                output = _CommandOutput(preview_limit_bytes=MAX_OUTPUT_PREVIEW_BYTES)
                wait_task = asyncio.create_task(process.wait())
                reader_tasks = [
                    asyncio.create_task(
                        self._read_stream(process.stdout, "stdout", output)
                    ),
                    asyncio.create_task(
                        self._read_stream(process.stderr, "stderr", output)
                    ),
                ]

                timed_out = False
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_task), timeout=normalized_timeout
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    await self._cleanup_process(process, wait_task)

                try:
                    await self._finish_reader_tasks(reader_tasks)
                finally:
                    output.close()

                return self._format_result(
                    exit_code=process.returncode,
                    output=output,
                    timeout=normalized_timeout,
                    timed_out=timed_out,
                    timeout_note=timeout_note,
                )

        except Exception as e:
            logger.error(f"执行命令失败: {e}", exc_info=True)
            return f"执行命令时发生错误: {str(e)}"
