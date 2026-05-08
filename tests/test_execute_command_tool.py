import asyncio
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
        match = re.search(r"临时文件: (.+)", result)
        self.assertIsNotNone(match)
        return match.group(1).strip()

    def _run_command(self, command: str, timeout: int = 60) -> str:
        tool = ExecuteCommandTool(session_id="session-1", user_id="10001")
        return asyncio.run(tool.run(command=command, timeout=timeout))

    def test_large_output_is_truncated_before_returning_to_agent(self):
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
        command = _python_command("print('ok')")

        result = self._run_command(command, timeout=9999)

        self.assertIn("timeout 参数超过上限", result)
        self.assertIn("ok", result)


if __name__ == "__main__":
    unittest.main()
