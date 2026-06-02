import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unittest

from app.agent.tools.impl.execute_command import (
    ExecuteCommandTool,
    MAX_OUTPUT_PREVIEW_BYTES,
)


def _python_command(code: str) -> str:
    """生成当前解释器可执行的 shell 命令，避免依赖系统 python 名称。"""
    args = [sys.executable, "-c", code]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(arg) for arg in args)


class TestExecuteCommandTool(unittest.TestCase):
    def _temp_file_path_from_result(self, result: str) -> str:
        """从工具返回文本中提取完整输出临时文件路径。"""
        match = re.search(r"临时文件: (.+)", result)
        self.assertIsNotNone(match)
        return match.group(1).strip()

    def _run_command(self, command: str, timeout: int = 60) -> str:
        """按一次性执行模式运行命令，兼容旧测试断言。"""
        tool = ExecuteCommandTool(session_id="session-1", user_id="10001")
        return asyncio.run(tool.run(action="run", command=command, timeout=timeout))

    def test_large_output_is_truncated_before_returning_to_agent(self):
        """大输出一次性命令只把预览返回给 Agent，并把完整内容写到临时文件。"""
        command = _python_command(
            "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"
        )

        result = self._run_command(command)
        temp_file_path = self._temp_file_path_from_result(result)

        self.addCleanup(lambda: os.path.exists(temp_file_path) and os.unlink(temp_file_path))
        self.assertIn("命令输出超过 10KB", result)
        self.assertIn("仅展示前 10KB 内容", result)
        self.assertIn("如需完整内容，请继续读取该文件", result)
        self.assertLess(len(result), MAX_OUTPUT_PREVIEW_BYTES + 600)

        with open(temp_file_path, encoding="utf-8") as file_handle:
            file_content = file_handle.read()

        self.assertIn("[标准输出]", file_content)
        self.assertGreater(len(file_content), 100000)

    def test_timeout_returns_partial_output_promptly(self):
        """一次性命令超时后应及时返回已经读取到的部分输出。"""
        command = _python_command(
            "import time; print('started', flush=True); time.sleep(5)"
        )

        started_at = time.monotonic()
        result = self._run_command(command, timeout=1)
        duration = time.monotonic() - started_at

        self.assertLess(duration, 4)
        self.assertIn("命令执行超时", result)
        self.assertIn("started", result)

    def test_timeout_with_large_output_writes_partial_full_log_to_temp_file(self):
        """超时且输出较大时，终止前完整输出应写入临时文件。"""
        command = _python_command(
            "import sys, time; sys.stdout.write('x' * 20000); sys.stdout.flush(); time.sleep(5)"
        )

        result = self._run_command(command, timeout=1)
        temp_file_path = self._temp_file_path_from_result(result)

        self.addCleanup(lambda: os.path.exists(temp_file_path) and os.unlink(temp_file_path))
        self.assertIn("命令执行超时", result)
        self.assertIn("截至命令终止前的完整输出已写入临时文件", result)

        with open(temp_file_path, encoding="utf-8") as file_handle:
            file_content = file_handle.read()

        self.assertIn("[标准输出]", file_content)
        self.assertGreaterEqual(file_content.count("x"), 20000)

    def test_timeout_is_capped(self):
        """一次性执行的 timeout 参数超过上限时应自动限幅。"""
        command = _python_command("print('ok')")

        result = self._run_command(command, timeout=9999)

        self.assertIn("timeout 参数超过上限", result)
        self.assertIn("ok", result)

    def test_forbidden_command_is_rejected(self):
        """明显危险命令在进入 shell 前应被拒绝。"""
        result = self._run_command("echo ok && rm -rf /")

        payload = json.loads(result)
        self.assertEqual(payload["status"], "error")
        # rm -rf / 命中删除根目录防护；断言拒绝原因点明 rm 根目录，避免锁死单一文案
        self.assertIn("不允许使用 rm", payload["error"])


class TestExecuteCommandSessionTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """创建每个测试复用的统一命令工具。"""
        self.tool = ExecuteCommandTool(session_id="session-1", user_id="10001")
        self._created_sessions: list[str] = []

    async def asyncTearDown(self):
        """清理测试中残留的后台会话，避免影响后续用例。"""
        for session_id in self._created_sessions:
            await self.tool.run(action="kill", session_id=session_id)

    @staticmethod
    def _loads(result: str) -> dict:
        """解析 execute_command 返回的 JSON 字符串。"""
        return json.loads(result)

    async def _start(self, command: str, *, use_pty: bool = False) -> dict:
        """通过 execute_command 启动后台会话并记录 ID。"""
        payload = self._loads(
            await self.tool.run(action="start", command=command, use_pty=use_pty)
        )
        session_id = payload.get("session_id")
        if session_id:
            self._created_sessions.append(session_id)
        return payload

    async def test_default_action_starts_session_promptly(self):
        """不传 action 时应默认后台启动，并快速返回会话 ID。"""
        command = _python_command(
            "import time; print('ready', flush=True); time.sleep(1); print('done', flush=True)"
        )

        started_at = time.monotonic()
        start_payload = self._loads(await self.tool.run(command=command, use_pty=False))
        duration = time.monotonic() - started_at
        self._created_sessions.append(start_payload["session_id"])

        self.assertLess(duration, 0.8)
        self.assertEqual(start_payload["status"], "running")
        self.assertIn("session_id", start_payload)

    async def test_read_and_wait_get_incremental_output(self):
        """同一个 execute_command 工具应能分段等待并读取增量输出。"""
        command = _python_command(
            "import time; print('ready', flush=True); time.sleep(1); print('done', flush=True)"
        )
        start_payload = await self._start(command)

        wait_payload = self._loads(
            await self.tool.run(
                action="wait",
                session_id=start_payload["session_id"],
                timeout_ms=200,
                since_seq=0,
            )
        )

        self.assertEqual(wait_payload["status"], "running")
        self.assertIn("ready", wait_payload["output"])

        final_payload = self._loads(
            await self.tool.run(
                action="wait",
                session_id=start_payload["session_id"],
                timeout_ms=3000,
                since_seq=wait_payload["output_until_seq"],
            )
        )

        self.assertEqual(final_payload["status"], "exited")
        self.assertEqual(final_payload["exit_code"], 0)
        self.assertIn("done", final_payload["output"])

    async def test_write_sends_input_to_running_process(self):
        """write 动作应能向后台进程 stdin 写入交互输入。"""
        command = _python_command(
            "line = input('name: '); print('hello ' + line, flush=True)"
        )
        start_payload = await self._start(command)

        await self.tool.run(
            action="write",
            session_id=start_payload["session_id"],
            input_text="moviepilot\n",
        )
        wait_payload = self._loads(
            await self.tool.run(
                action="wait",
                session_id=start_payload["session_id"],
                timeout_ms=3000,
                since_seq=0,
            )
        )

        self.assertEqual(wait_payload["status"], "exited")
        self.assertIn("hello moviepilot", wait_payload["output"])

    async def test_kill_stops_long_running_process(self):
        """kill 动作应能终止长时间运行的后台命令会话。"""
        command = _python_command(
            "import time; print('started', flush=True); time.sleep(20)"
        )
        start_payload = await self._start(command)

        read_payload = self._loads(
            await self.tool.run(
                action="wait",
                session_id=start_payload["session_id"],
                timeout_ms=500,
                since_seq=0,
            )
        )
        kill_payload = self._loads(
            await self.tool.run(action="kill", session_id=start_payload["session_id"])
        )

        self.assertIn("started", read_payload["output"])
        self.assertIn(kill_payload["status"], {"killed", "exited"})
