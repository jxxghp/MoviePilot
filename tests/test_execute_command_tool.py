"""命令工具的输出归档、超时、取消和交互回归，逐测试隔离终端 owner。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.tools.impl import execute_command as command_module
from app.agent.tools.impl.execute_command import MAX_OUTPUT_PREVIEW_BYTES, ExecuteCommandTool

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("terminal_scope")]


def _python_command(code: str) -> str:
    """生成当前虚拟环境解释器的 shell 命令，避免依赖系统 python 名称。"""
    args = [sys.executable, "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _archive_path(result: str) -> Path:
    """从结构化回执取得归档路径，缺失归档不能被当作完整输出。"""
    path = json.loads(result)["output_file"]
    assert path
    return Path(path)


@pytest_asyncio.fixture
async def command_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[ExecuteCommandTool]:
    """每个测试独占 manager、并发信号量和归档文件，退出时完整回收。"""
    manager = _TerminalSessionManager()
    archives = []
    original_temporary_file = command_module.NamedTemporaryFile

    def temporary_file(*args: Any, **kwargs: Any) -> Any:
        """仅改变归档存放位置，保留真实文件和生产归档写入逻辑。"""
        kwargs["dir"] = tmp_path
        archive = original_temporary_file(*args, **kwargs)
        archives.append(archive)
        return archive

    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    monkeypatch.setattr(command_module, "_command_semaphore", asyncio.Semaphore(command_module.COMMAND_CONCURRENCY_LIMIT))
    monkeypatch.setattr(command_module, "NamedTemporaryFile", temporary_file)
    tool = ExecuteCommandTool(session_id="session-1", user_id="10001")
    tool.set_agent_context({"is_admin": True, "should_dispatch_reply": False})
    try:
        yield tool
    finally:
        try:
            await asyncio.wait_for(manager.close(), timeout=10)
        finally:
            for archive in archives:
                archive.close()
                Path(archive.name).unlink(missing_ok=True)


async def _start(tool: ExecuteCommandTool, command: str, *, use_pty: bool = False) -> dict[str, Any]:
    """经工具入口启动真实会话，生命周期由测试独占的 manager 回收。"""
    return json.loads(await tool.run(action="start", command=command, use_pty=use_pty))


async def _write_input(tool: ExecuteCommandTool, session_id: str, text: str) -> None:
    """确认 stdin 写入已被接收，避免把接口错误掩盖成后续读取超时。"""
    payload = json.loads(await tool.run(action="write", session_id=session_id, input_text=text))
    assert payload.get("written_bytes") == len(text.encode("utf-8")), payload


async def _wait_until_complete(
    tool: ExecuteCommandTool, session_id: str, since_seq: int = 0, since_offset: int = 0,
) -> dict[str, Any]:
    """按真实消费游标排空尾部，不把首批输出误当成进程和读取器均已结束。"""
    output = []
    for _ in range(10):
        payload = json.loads(await tool.run(
            action="wait", session_id=session_id, timeout_ms=3000,
            since_seq=since_seq, since_offset=since_offset,
        ))
        output.append(payload["output"])
        since_seq, since_offset = payload["output_until_seq"], payload["output_until_offset"]
        if payload["output_complete"] and since_seq == payload["last_seq"] and since_offset == 0:
            return {**payload, "output": "".join(output)}
    raise AssertionError("命令未在有限读取次数内完成输出")


async def test_large_output_is_truncated_before_returning_to_agent(command_tool: ExecuteCommandTool) -> None:
    """大输出保留头尾预览，并将完整内容归档到真实文件。"""
    command = _python_command("import sys; sys.stdout.write('HEAD-' + 'x' * 200000 + '-TAIL'); sys.stdout.flush()")
    result = await command_tool.run(action="run", command=command, timeout=60)
    path = _archive_path(result)
    assert "命令输出超过 32KB" in result
    assert "仅展示前后各 16KB 内容" in result
    assert "如需完整内容，请继续读取该文件" in result
    assert "HEAD-" in result and "-TAIL" in result
    assert len(result) < MAX_OUTPUT_PREVIEW_BYTES + 1200
    content = path.read_text(encoding="utf-8")
    assert "[标准输出]" in content
    assert "HEAD-" in content and "-TAIL" in content
    assert len(content) > 100000


async def test_timeout_returns_partial_output_promptly(command_tool: ExecuteCommandTool) -> None:
    """真实执行超时应及时返回终止前输出，保留原有四秒上限断言。"""
    command = _python_command("import time; print('started', flush=True); time.sleep(5)")
    started_at = time.monotonic()
    result = await command_tool.run(action="run", command=command, timeout=1)
    assert time.monotonic() - started_at < 4
    assert "命令执行超时" in result
    assert "started" in result


async def test_cancelled_run_cleans_up_process(
    command_tool: ExecuteCommandTool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真正的 exec 子进程创建后用 Event 取消，返回取消前必须清理该进程。"""
    original_create = asyncio.create_subprocess_exec
    created = asyncio.Event()
    processes = []

    async def create_process(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        """保留真实启动，仅在拿到进程对象后通知取消测试。"""
        process = await original_create(*args, **kwargs)
        processes.append(process)
        created.set()
        return process

    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", create_process)
    task = asyncio.create_task(command_tool.run(
        action="run", command=_python_command("import time; time.sleep(20)"), timeout=60,
    ))
    try:
        await asyncio.wait_for(created.wait(), timeout=5)
        assert processes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_timeout_with_large_output_writes_partial_full_log_to_temp_file(command_tool: ExecuteCommandTool) -> None:
    """超时大输出仍把终止前的完整内容归档，不能只留下截断预览。"""
    command = _python_command("import sys, time; sys.stdout.write('x' * 60000); sys.stdout.flush(); time.sleep(5)")
    result = await command_tool.run(action="run", command=command, timeout=1)
    path = _archive_path(result)
    assert "命令执行超时" in result
    assert "截至命令终止前的完整输出已写入临时文件" in result
    content = path.read_text(encoding="utf-8")
    assert "[标准输出]" in content
    assert content.count("x") >= 60000


async def test_timeout_is_capped(command_tool: ExecuteCommandTool) -> None:
    """超过上限的 timeout 限幅，同时真实短命令仍正常执行。"""
    result = await command_tool.run(action="run", command=_python_command("print('ok')"), timeout=9999)
    assert "timeout 参数超过上限" in result
    assert "ok" in result


async def test_forbidden_command_is_rejected(command_tool: ExecuteCommandTool) -> None:
    """根目录删除命令必须在启动前拒绝，并保留显式确认提示。"""
    result = await command_tool.run(action="run", command="echo ok && rm -rf /", timeout=60)
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "confirm_dangerous=true" in payload["error"]


async def test_dangerous_command_requires_explicit_confirmation(command_tool: ExecuteCommandTool) -> None:
    """拒绝真实高危命令；确认分支仅运行打印文案的无害 Python。"""
    rejected = await command_tool.run(action="run", command="echo ok && shutdown now", timeout=1)
    allowed = await command_tool.run(
        action="run", command=_python_command("print('shutdown now confirmed')"), timeout=1, confirm_dangerous=True,
    )
    payload = json.loads(rejected)
    assert payload["status"] == "error"
    assert "confirm_dangerous=true" in payload["error"]
    assert "shutdown now confirmed" in allowed


async def test_default_action_starts_session_promptly(command_tool: ExecuteCommandTool) -> None:
    """省略 action 时快速返回运行中会话，保留原有 0.8 秒行为上限。"""
    command = _python_command("print('ready', flush=True); input()")
    started_at = time.monotonic()
    payload = json.loads(await command_tool.run(command=command, use_pty=False))
    assert time.monotonic() - started_at < 0.8
    assert payload["status"] == "running"
    assert "session_id" in payload


async def test_read_and_wait_get_incremental_output(command_tool: ExecuteCommandTool) -> None:
    """READY 握手后再放行最后输出，验证同一工具的分段等待和消费位置。"""
    initial = await _start(command_tool, _python_command("print('ready', flush=True); input(); print('done', flush=True)"))
    waiting = json.loads(await command_tool.run(
        action="wait", session_id=initial["session_id"], timeout_ms=200, since_seq=0,
    ))
    assert waiting["status"] == "running"
    assert "ready" in waiting["output"]
    await _write_input(command_tool, initial["session_id"], "continue\n")
    final = await _wait_until_complete(
        command_tool, initial["session_id"],
        since_seq=waiting["output_until_seq"], since_offset=waiting["output_until_offset"],
    )
    assert final["status"] == "exited"
    assert final["exit_code"] == 0
    assert "done" in final["output"]


async def test_write_sends_input_to_running_process(command_tool: ExecuteCommandTool) -> None:
    """write 向真实 stdin 发送交互输入，尾部结果可完整读取。"""
    initial = await _start(command_tool, _python_command("line = input('name: '); print('hello ' + line, flush=True)"))
    await _write_input(command_tool, initial["session_id"], "moviepilot\n")
    final = await _wait_until_complete(command_tool, initial["session_id"])
    assert final["status"] == "exited"
    assert "hello moviepilot" in final["output"]


async def test_kill_stops_long_running_process(command_tool: ExecuteCommandTool) -> None:
    """读到启动标记后终止长命令，保持原有终态断言。"""
    initial = await _start(command_tool, _python_command("import time; print('started', flush=True); time.sleep(20)"))
    observed = json.loads(await command_tool.run(
        action="wait", session_id=initial["session_id"], timeout_ms=500, since_seq=0,
    ))
    killed = json.loads(await command_tool.run(action="kill", session_id=initial["session_id"]))
    assert "started" in observed["output"]
    assert killed["status"] in {"killed", "exited"}
