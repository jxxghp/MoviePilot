"""从真实命令工具入口验证统一启动目录、解释器及跨分片文本输入输出。"""

import asyncio
import json
import os
import shlex
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.agent.loader import close_materialized_terminal_sessions
from app.agent.terminal import manager as terminal_module
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.tools.impl import execute_command as command_module
from app.agent.tools.impl.execute_command import ExecuteCommandTool, _CommandOutput


def _python(code: str) -> str:
    """使用当前解释器和逐参数转义，真实程序仅接触临时目录及标准流。"""
    return shlex.join([sys.executable, "-c", code])


@pytest_asyncio.fixture
async def command_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[ExecuteCommandTool, Path]]:
    """父进程 cwd 与项目根明确分离，所有后台进程由独立 manager 统一回收。"""
    root_dir, parent_dir = tmp_path / "project", tmp_path / "parent"
    root_dir.mkdir()
    parent_dir.mkdir()
    (root_dir / "relative").mkdir()
    monkeypatch.chdir(parent_dir)
    original_setting = command_module.get_runtime_setting

    def setting(name: str) -> Any:
        """只替换目录配置，其他运行契约仍走测试已经隔离的宿主配置。"""
        return root_dir if name == "ROOT_PATH" else original_setting(name)

    manager = _TerminalSessionManager()
    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    monkeypatch.setattr(command_module, "get_runtime_setting", setting)
    monkeypatch.setattr(terminal_module, "get_runtime_setting", setting)
    monkeypatch.setattr(command_module, "_command_semaphore", asyncio.Semaphore(2))
    tool = ExecuteCommandTool(session_id="command-io", user_id="command-io-owner")
    try:
        yield tool, root_dir
    finally:
        await manager.close()


async def _finish(tool: ExecuteCommandTool, payload: dict[str, Any]) -> dict[str, Any]:
    """持续按已消费字节游标读取，直到进程与读取器都收尾。"""
    chunks = [payload["output"]]
    async with asyncio.timeout(5):
        while not (payload["output_complete"] and payload["output_until_seq"] == payload["last_seq"]
                   and payload["output_until_offset"] == 0):
            payload = json.loads(await tool.run(
                action="wait", session_id=payload["session_id"], timeout_ms=1000,
                since_seq=payload["output_until_seq"], since_offset=payload["output_until_offset"],
            ))
            chunks.append(payload["output"])
    return {**payload, "output": "".join(chunks)}


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 解释器与 PTY 合同")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "pipe", "pty"])
@pytest.mark.parametrize("directory", ["default", "relative", "absolute"])
async def test_all_launch_modes_share_directory_shell_and_environment(command_environment, mode: str, directory: str) -> None:
    """相同参数不能因为是否保留会话或使用 PTY 而切换目录、解释器或环境。"""
    tool, root_dir = command_environment
    cwd = {"default": None, "relative": "relative", "absolute": str(root_dir / "relative")}[directory]
    expected_cwd = root_dir if directory == "default" else root_dir / "relative"
    code = "import os; print('DIRECTORY=' + os.getcwd()); print('MARKER=' + os.environ['MOVIEPILOT_IO_PROBE'])"
    payload = json.loads(await tool.run(
        action="run" if mode == "run" else "start", command=_python(code), cwd=cwd,
        env={"SHELL": "/bin/sh", "MOVIEPILOT_IO_PROBE": "launch-value"},
        use_pty=mode == "pty", timeout=5, since_offset=0,
    ))
    if mode != "run":
        payload = await _finish(tool, payload)
    assert payload["execution_outcome"] == "succeeded"
    assert payload["cwd"] == str(expected_cwd.resolve())
    assert Path(payload["shell"]).resolve() == Path("/bin/sh").resolve()
    assert payload["login"] is False
    assert f"DIRECTORY={expected_cwd.resolve()}" in payload["output"]
    assert "MARKER=launch-value" in payload["output"]
    assert "MOVIEPILOT_IO_PROBE" not in os.environ


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 显式解释器合同")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "pipe", "pty"])
async def test_explicit_shell_wins_over_environment_without_implicit_login(command_environment, mode: str) -> None:
    """显式 bash 能运行其语言特性，不能被 env.SHELL 或 PTY 的旧登录路径替换。"""
    tool, _ = command_environment
    payload = json.loads(await tool.run(
        action="run" if mode == "run" else "start", command="items=(one two); printf 'ARRAY:%s\\n' \"${items[1]}\"",
        env={"SHELL": "/bin/sh"}, shell="/bin/bash", login=False, use_pty=mode == "pty", since_offset=0,
    ))
    if mode != "run":
        payload = await _finish(tool, payload)
    assert payload["exit_code"] == 0
    assert "ARRAY:two" in payload["output"]
    assert Path(payload["shell"]).resolve() == Path("/bin/bash").resolve()
    assert payload["login"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "start"])
async def test_invalid_directory_is_rejected_before_process_creation(command_environment, monkeypatch, mode: str) -> None:
    """目录参数错误必须保持零进程，不允许先启动后再用回包补救。"""
    tool, root_dir = command_environment
    create = AsyncMock(side_effect=AssertionError("invalid cwd created a process"))
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", create)
    payload = json.loads(await tool.run(action=mode, command="echo never", cwd=str(root_dir / "absent"), use_pty=False))
    assert payload["success"] is False and payload["execution_outcome"] == "failed"
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"\xad", b""])
async def test_run_decodes_split_utf8_at_eof_without_replacing_complete_characters(ending: bytes) -> None:
    """Event 固定两次真实 StreamReader 读取边界，完整字符拼接和末尾残缺各有明确结果。"""
    reader = asyncio.StreamReader()
    first_read = asyncio.Event()
    original_read = reader.read

    async def read(size: int) -> bytes:
        """读取器消费首段后才允许测试送第二段，排除同批合并造成的空过。"""
        data = await original_read(size)
        if data == b"\xe4\xb8":
            first_read.set()
        return data

    reader.read = read
    output = _CommandOutput(preview_limit_bytes=1024)
    task = asyncio.create_task(ExecuteCommandTool._read_stream(reader, "stdout", output))
    try:
        reader.feed_data(b"\xe4\xb8")
        await asyncio.wait_for(first_read.wait(), timeout=2)
        assert output.stdout == ""
        if ending:
            reader.feed_data(ending)
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2)
        assert output.stdout == ("中" if ending else "\ufffd")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        output.close()


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX shell 与 pipe EOF")
@pytest.mark.asyncio
async def test_tool_write_close_stdin_delivers_final_input_and_retains_output(command_environment) -> None:
    """模型通过工具写末段和 EOF 后仍能读取最终计算结果与退出状态。"""
    tool, _ = command_environment
    initial = json.loads(await tool.run(
        action="start", command=_python("import sys; data=sys.stdin.read(); print('RECEIVED=' + data, flush=True)"),
        env={"SHELL": "/bin/sh"}, use_pty=False, since_offset=0, yield_time_ms=0,
    ))
    closed = json.loads(await tool.run(action="write", session_id=initial["session_id"], input_text="完整输入", close_stdin=True,
                                       since_seq=0, since_offset=0))
    assert closed["stdin_closed"] is True
    assert closed["written_bytes"] == len("完整输入".encode("utf-8"))
    final = await _finish(tool, closed)
    assert final["execution_outcome"] == "succeeded" and final["output_complete"] is True
    assert "RECEIVED=完整输入" in final["output"]


@pytest.mark.asyncio
async def test_tool_interrupt_routes_to_one_shot_control_without_kill(monkeypatch) -> None:
    """新工具动作必须调用独立中断路径，不能复用会升级强杀的 kill。"""
    manager = _TerminalSessionManager()
    manager.interrupt = AsyncMock(return_value={"signal_sent": True, "signal": "SIGINT"})
    manager.kill = AsyncMock(side_effect=AssertionError("interrupt called kill"))
    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    result = json.loads(await ExecuteCommandTool(session_id="io", user_id="owner").run(action="interrupt", session_id="term-test"))
    assert result["signal_sent"] is True
    manager.interrupt.assert_awaited_once()
    manager.kill.assert_not_awaited()


def test_run_preview_boundary_keeps_valid_unicode_and_complete_archive(tmp_path: Path, monkeypatch) -> None:
    """预览字节限额切到完整字符中间时不制造替换字，原始字符仍可在尾部和归档读取。"""
    original = command_module.NamedTemporaryFile

    def archive(**kwargs: Any) -> Any:
        """把完整输出归档限制在当前测试目录。"""
        return original(dir=tmp_path, **kwargs)

    monkeypatch.setattr(command_module, "NamedTemporaryFile", archive)
    output = _CommandOutput(preview_limit_bytes=2)
    try:
        output.append("stdout", "中文")
        output.close()
        assert output.preview_truncated is True
        assert output.captured_bytes <= 2
        assert "\ufffd" not in output.combined_preview
        assert "中文" in output.combined_preview
        assert "中文" in Path(output.temp_file_path).read_text(encoding="utf-8")
    finally:
        output.close()


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 管道关停")
@pytest.mark.asyncio
async def test_lazy_shutdown_closes_the_materialized_terminal_package_owner(command_environment, monkeypatch) -> None:
    """模块迁移后宿主关停仍必须找到实际 manager 并回收已启动进程。"""
    tool, _ = command_environment
    manager = command_module.get_terminal_session_manager()
    monkeypatch.setattr(terminal_module, "terminal_session_manager", manager)
    payload = json.loads(await tool.run(
        action="start", command=_python("print('READY', flush=True); input()"), use_pty=False,
        env={"SHELL": "/bin/sh"}, since_offset=0,
    ))
    session = manager.get_session(payload["session_id"])
    assert session.process is not None and session.process.returncode is None
    await close_materialized_terminal_sessions()
    assert manager._closed is True and manager._sessions == {}
    assert session.process.returncode is not None


@pytest.mark.asyncio
async def test_lazy_shutdown_does_not_import_an_unmaterialized_terminal_manager(monkeypatch) -> None:
    """未使用命令工具时，关停查表不能顺带创建新的终端管理器。"""
    monkeypatch.delitem(sys.modules, "app.agent.terminal.manager", raising=False)
    await close_materialized_terminal_sessions()
    assert "app.agent.terminal.manager" not in sys.modules
