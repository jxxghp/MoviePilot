import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.llm.capability import OpenAIChatAudioProvider
from app.agent.mcp import _StdioMcpSession
from app.agent.prompt import PromptManager
from app.agent.shell import (
    AgentShell,
    agent_text_subprocess_kwargs,
    build_agent_subprocess_env,
    resolve_agent_shell,
)
from app.agent.tools.impl._terminal_session import _TerminalSessionManager
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.impl.service import _run_service_script
from app.schemas.agent import AgentMcpServerConfig


def _finder(paths: dict[str, str]):
    """构造只返回测试声明命令路径的 which 替身。"""
    return lambda command: paths.get(command)


def _fake_process(stdout: bytes = b"", stderr: bytes = b""):
    """构造满足 Agent 异步子进程读取合同的最小进程替身。"""
    stdout_reader = asyncio.StreamReader()
    stdout_reader.feed_data(stdout)
    stdout_reader.feed_eof()
    stderr_reader = asyncio.StreamReader()
    stderr_reader.feed_data(stderr)
    stderr_reader.feed_eof()

    process = SimpleNamespace(
        pid=12345,
        returncode=0,
        stdin=None,
        stdout=stdout_reader,
        stderr=stderr_reader,
        terminate=lambda: None,
        kill=lambda: None,
    )

    async def wait() -> int:
        """立即返回成功退出码。"""
        return 0

    process.wait = wait
    return process


def test_windows_shell_prefers_git_bash(tmp_path: Path) -> None:
    """Windows 检测到 Git for Windows 时应优先使用同目录 Git Bash。"""
    git_root = tmp_path / "Git"
    git_path = git_root / "cmd" / "git.exe"
    bash_path = git_root / "bin" / "bash.exe"
    git_path.parent.mkdir(parents=True)
    bash_path.parent.mkdir(parents=True)
    git_path.touch()
    bash_path.touch()

    shell = resolve_agent_shell(
        platform_name="nt",
        command_finder=_finder({"git": str(git_path), "pwsh": "ignored"}),
        environment={},
    )

    assert shell is not None
    assert shell.kind == "git-bash"
    assert shell.build_argv("git status") == [
        str(bash_path),
        "--noprofile",
        "--norc",
        "-lc",
        "git status",
    ]
    assert "Git Bash" in shell.prompt_guidance()
    assert str(bash_path) not in shell.prompt_guidance()


def test_windows_shell_uses_pwsh_when_git_is_unavailable() -> None:
    """Windows 没有 Git 时应统一选择 PowerShell 7，而不是旧 powershell.exe。"""
    shell = resolve_agent_shell(
        platform_name="nt",
        command_finder=_finder({"pwsh": "C:/PowerShell/pwsh.exe"}),
        environment={},
    )

    assert shell is not None
    assert shell.kind == "pwsh"
    argv = shell.build_argv("Get-ChildItem")
    assert argv[:4] == [
        "C:/PowerShell/pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
    ]
    assert "[Console]::OutputEncoding" in argv[-1]
    assert "chcp.com 65001" in argv[-1]
    assert argv[-1].endswith("Get-ChildItem")
    assert "PowerShell 7" in shell.prompt_guidance()


def test_windows_shell_keeps_git_priority_when_only_pwsh_can_host_commands() -> None:
    """Git 存在但无法定位 Git Bash 时，pwsh 仍应提示仓库操作优先使用 git。"""
    shell = resolve_agent_shell(
        platform_name="nt",
        command_finder=_finder(
            {"git": "C:/Portable/git.exe", "pwsh": "C:/PowerShell/pwsh.exe"}
        ),
        environment={},
    )

    assert shell is not None
    assert shell.kind == "pwsh"
    assert shell.git_available is True
    assert "优先使用 `git`" in shell.prompt_guidance()


def test_windows_shell_uses_utf8_cmd_fallback_without_git_or_pwsh() -> None:
    """Git Bash 与 pwsh 都不可用时应保留显式 UTF-8 的 cmd 兼容回退。"""
    shell = resolve_agent_shell(
        platform_name="nt",
        command_finder=_finder({}),
        environment={"COMSPEC": "C:/Windows/System32/cmd.exe"},
    )

    assert shell is not None
    assert shell.kind == "cmd"
    assert shell.build_argv("dir")[-1] == "chcp 65001>nul &\ndir"


def test_posix_shell_policy_preserves_native_shell_and_environment(monkeypatch) -> None:
    """Linux/macOS 不应被 Windows shell 选择或 UTF-8 环境覆盖改变。"""
    monkeypatch.setenv("PYTHONIOENCODING", "locale-default")

    assert resolve_agent_shell(platform_name="posix") is None
    environment = build_agent_subprocess_env(
        {"CUSTOM": "value"}, platform_name="posix"
    )
    assert environment["PYTHONIOENCODING"] == "locale-default"
    assert environment["CUSTOM"] == "value"
    assert agent_text_subprocess_kwargs(platform_name="posix") == {}


def test_windows_subprocess_environment_forces_utf8_after_overrides() -> None:
    """Windows 调用方不能通过自定义环境重新引入本地默认编码。"""
    environment = build_agent_subprocess_env(
        {
            "PYTHONIOENCODING": "cp936",
            "PYTHONLEGACYWINDOWSSTDIO": "1",
            "PYTHONUTF8": "0",
            "LANG": "zh_CN.GBK",
        },
        platform_name="nt",
    )

    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert "PYTHONLEGACYWINDOWSSTDIO" not in environment


def test_prompt_injects_selected_windows_shell_without_executable_path() -> None:
    """模型提示词应知道实际 Windows shell，但不能暴露安装绝对路径。"""
    shell = AgentShell(
        kind="pwsh",
        executable="C:/secret/location/pwsh.exe",
        arguments=("-Command",),
    )
    with patch("app.agent.prompt.resolve_agent_shell", return_value=shell):
        moviepilot_info = PromptManager()._get_moviepilot_info()

    assert "PowerShell 7" in moviepilot_info
    assert "C:/secret/location/pwsh.exe" not in moviepilot_info


@pytest.mark.anyio
async def test_execute_command_uses_selected_windows_shell_and_utf8_environment() -> None:
    """一次性命令在 Windows 策略下应绕过隐式 cmd 并传入 UTF-8 环境。"""
    shell = AgentShell(
        kind="pwsh",
        executable="pwsh.exe",
        arguments=("-Command",),
        command_prefix="utf8-prefix;",
    )
    process = _fake_process(stdout="中文输出".encode("utf-8"))
    create_exec = AsyncMock(return_value=process)
    tool = ExecuteCommandTool(session_id="session", user_id="user")

    with (
        patch(
            "app.agent.tools.impl.execute_command.resolve_agent_shell",
            return_value=shell,
        ),
        patch(
            "app.agent.tools.impl.execute_command.build_agent_subprocess_env",
            return_value={"PYTHONUTF8": "1"},
        ),
        patch(
            "app.agent.tools.impl.execute_command.asyncio.create_subprocess_exec",
            create_exec,
        ),
        patch(
            "app.agent.tools.impl.execute_command.asyncio.create_subprocess_shell"
        ) as create_shell,
    ):
        result = await tool.run(action="run", command="Get-Location", timeout=1)

    assert "中文输出" in result
    assert create_exec.await_args.args == (
        "pwsh.exe",
        "-Command",
        "utf8-prefix;\nGet-Location",
    )
    assert create_exec.await_args.kwargs["env"] == {"PYTHONUTF8": "1"}
    create_shell.assert_not_called()


@pytest.mark.anyio
async def test_terminal_session_uses_selected_windows_shell() -> None:
    """后台会话与一次性命令必须共享同一 Windows shell 策略。"""
    shell = AgentShell(
        kind="git-bash",
        executable="bash.exe",
        arguments=("-lc",),
    )
    process = _fake_process()
    create_exec = AsyncMock(return_value=process)
    manager = _TerminalSessionManager()

    with (
        patch(
            "app.agent.tools.impl._terminal_session.resolve_agent_shell",
            return_value=shell,
        ),
        patch(
            "app.agent.tools.impl._terminal_session.asyncio.create_subprocess_exec",
            create_exec,
        ),
        patch(
            "app.agent.tools.impl._terminal_session.asyncio.create_subprocess_shell"
        ) as create_shell,
    ):
        session = await manager._start_pipe_session(
            "git status", "C:/MoviePilot", {"PYTHONUTF8": "1"}
        )
        assert session.wait_task is not None
        await session.wait_task

    assert create_exec.await_args.args == ("bash.exe", "-lc", "git status")
    assert create_exec.await_args.kwargs["env"] == {"PYTHONUTF8": "1"}
    create_shell.assert_not_called()


@pytest.mark.anyio
async def test_stdio_mcp_inherits_agent_utf8_environment() -> None:
    """stdio MCP 子进程也必须继承 Windows Agent 的 UTF-8 环境。"""
    process = _fake_process()
    create_exec = AsyncMock(return_value=process)
    server = AgentMcpServerConfig(
        id="fake",
        name="Fake MCP",
        transport="stdio",
        command="server.exe",
        env={"PYTHONIOENCODING": "cp936"},
    )
    session = _StdioMcpSession(server)

    with (
        patch(
            "app.agent.mcp.build_agent_subprocess_env",
            return_value={"PYTHONIOENCODING": "utf-8"},
        ) as build_env,
        patch("app.agent.mcp.asyncio.create_subprocess_exec", create_exec),
    ):
        await session.__aenter__()
        await session.__aexit__(None, None, None)

    build_env.assert_called_once_with(server.env)
    assert create_exec.await_args.kwargs["env"] == {"PYTHONIOENCODING": "utf-8"}


def test_service_script_uses_windows_utf8_text_subprocess_kwargs(tmp_path: Path) -> None:
    """固定 Skill Python 子进程应显式使用 UTF-8 文本解码和环境。"""
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"success": True}, ensure_ascii=False),
        stderr="",
    )
    with (
        patch(
            "app.agent.tools.impl.service.get_runtime_setting",
            return_value=tmp_path,
        ),
        patch(
            "app.agent.tools.impl.service.agent_text_subprocess_kwargs",
            return_value={
                "encoding": "utf-8",
                "errors": "replace",
                "env": {"PYTHONUTF8": "1"},
            },
        ),
        patch(
            "app.agent.tools.impl.service.subprocess.run",
            return_value=completed,
        ) as runner,
    ):
        payload = _run_service_script(
            relative_script="skills/demo.py",
            selector_flag=None,
            selector_value=None,
            action="list",
            arguments={},
        )

    assert payload["success"] is True
    assert runner.call_args.kwargs["encoding"] == "utf-8"
    assert runner.call_args.kwargs["errors"] == "replace"
    assert runner.call_args.kwargs["env"] == {"PYTHONUTF8": "1"}


def test_audio_ffmpeg_uses_windows_utf8_text_subprocess_kwargs(tmp_path: Path) -> None:
    """Agent 音频 ffmpeg 文本输出也应走统一的 Windows UTF-8 解码参数。"""
    input_path = tmp_path / "input.wav"
    input_path.write_bytes(b"wav")
    expected_output = input_path.with_suffix(".opus")

    def run_ffmpeg(_command, **_kwargs):
        """模拟 ffmpeg 成功生成输出文件。"""
        expected_output.write_bytes(b"opus")
        return subprocess.CompletedProcess([], 0, "", "")

    provider = OpenAIChatAudioProvider()
    with (
        patch(
            "app.agent.llm.capability.shutil.which",
            return_value="/usr/bin/ffmpeg",
        ),
        patch(
            "app.agent.llm.capability.agent_text_subprocess_kwargs",
            return_value={
                "encoding": "utf-8",
                "errors": "replace",
                "env": {"PYTHONUTF8": "1"},
            },
        ),
        patch(
            "app.agent.llm.capability.subprocess.run",
            side_effect=run_ffmpeg,
        ) as runner,
    ):
        output_path = provider._convert_wav_to_opus(input_path)

    assert output_path == expected_output
    assert runner.call_args.kwargs["encoding"] == "utf-8"
    assert runner.call_args.kwargs["errors"] == "replace"
    assert runner.call_args.kwargs["env"] == {"PYTHONUTF8": "1"}
