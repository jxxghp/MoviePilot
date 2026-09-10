"""Agent 解释器、登录模式、工作目录及跨平台子进程契约。"""

import asyncio
import json
import ntpath
import os
import shutil
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
    resolve_agent_cwd,
    resolve_agent_shell,
)
from app.agent.terminal.manager import _TerminalSessionManager
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


def test_windows_shell_prefers_git_bash() -> None:
    """Windows 检测到 Git for Windows 时应优先使用同目录 Git Bash。"""
    git_path = "C:/Git/cmd/git.exe"
    bash_path = "C:/Git/bin/bash.exe"
    with patch("app.agent.shell.Path.is_file", lambda candidate: str(candidate).replace("\\", "/") == bash_path):
        shell = resolve_agent_shell(
            platform_name="nt", cwd="C:/project",
            command_finder=_finder({"git": git_path, "pwsh": "ignored"}),
            environment={},
        )

    assert shell is not None
    assert shell.kind == "git-bash"
    assert shell.build_argv("git status") == [
        str(Path(bash_path)),
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

    shell = resolve_agent_shell(platform_name="posix", environment={"SHELL": "/bin/sh"})
    assert shell.kind == "sh" and shell.login is False
    assert shell.build_argv("pwd") == ["/bin/sh", "-c", "pwd"]
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
            "app.agent.terminal.manager.resolve_agent_shell",
            return_value=shell,
        ),
        patch(
            "app.agent.terminal.manager.asyncio.create_subprocess_exec",
            create_exec,
        ),
        patch(
            "app.agent.terminal.manager.asyncio.create_subprocess_shell"
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


def test_posix_explicit_shell_overrides_environment_and_defaults_to_non_login() -> None:
    """显式解释器优先于环境，默认登录模式不得因后台或 PTY 路径发生变化。"""
    shell = resolve_agent_shell(platform_name="posix", executable="bash", environment={"SHELL": "/bin/zsh"},
                                command_finder=_finder({"bash": "/custom/bin/bash"}))
    assert shell.kind == "bash" and shell.executable == "/custom/bin/bash" and shell.login is False
    assert shell.build_argv("printf '%s' value") == ["/custom/bin/bash", "-c", "printf '%s' value"]
    assert "Bash" in shell.prompt_guidance() and "非登录模式" in shell.prompt_guidance()
    assert shell.executable not in shell.prompt_guidance()


def test_posix_shell_uses_child_environment_then_host_then_sh(monkeypatch) -> None:
    """子进程指定 SHELL 优先，未指定时沿用宿主，双方缺失才使用 /bin/sh。"""
    finder = _finder({"/bin/zsh": "/bin/zsh", "/bin/bash": "/bin/bash", "/bin/sh": "/bin/sh"})
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert resolve_agent_shell(platform_name="posix", environment={"SHELL": "/bin/zsh"}, command_finder=finder).kind == "zsh"
    assert resolve_agent_shell(platform_name="posix", environment={}, command_finder=finder).kind == "bash"
    monkeypatch.delenv("SHELL")
    assert resolve_agent_shell(platform_name="posix", environment={}, command_finder=finder).kind == "sh"


@pytest.mark.parametrize("kind", ["sh", "bash", "zsh", "dash", "ash", "ksh", "mksh", "fish"])
def test_posix_login_is_explicit_and_preserves_command_as_single_argument(kind: str) -> None:
    """登录选项改变解释器 argv，用户命令仍是原样单个参数，不经过第二层 shell。"""
    command = "printf '%s\\n' '$HOME; literal'"
    shell = resolve_agent_shell(platform_name="posix", executable=kind, login=True,
                                command_finder=_finder({kind: f"/shells/{kind}"}), environment={})
    assert shell.login is True and shell.build_argv(command) == [f"/shells/{kind}", "-lc", command]
    assert "登录模式" in shell.prompt_guidance()


@pytest.mark.parametrize("kind", ["csh", "tcsh"])
def test_c_shell_rejects_login_command_combination_before_start(kind: str) -> None:
    """C shell 登录参数不能与 -c 组合，应在 fork/exec 前明确失败。"""
    finder = _finder({kind: f"/shells/{kind}"})
    assert resolve_agent_shell(platform_name="posix", executable=kind, command_finder=finder).login is False
    with pytest.raises(ValueError, match="不支持 login=True"):
        resolve_agent_shell(platform_name="posix", executable=kind, login=True, command_finder=finder)


@pytest.mark.parametrize("shell", ["", "  ", "missing-shell", "bash -l", "bad\x00path"])
def test_invalid_explicit_shell_is_rejected_before_process_creation(shell: str) -> None:
    """缺失解释器与带参数字符串不能回退到另一种命令语言执行。"""
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_agent_shell(platform_name="posix", executable=shell, command_finder=_finder({}))


@pytest.mark.parametrize("login", ["true", 1, [], {}])
def test_invalid_login_type_cannot_be_coerced_into_profile_execution(login) -> None:
    """底层公共策略只接受明确布尔值，避免字符串或容器意外启用登录脚本。"""
    with pytest.raises(ValueError, match="布尔值"):
        resolve_agent_shell(platform_name="posix", executable="sh", login=login, command_finder=_finder({"sh": "/bin/sh"}))


def test_unknown_interpreter_language_is_not_guessed() -> None:
    """即使程序可找到，也不能把 Python 等任意可执行文件当 POSIX shell 使用。"""
    with pytest.raises(ValueError, match="不支持的命令解释器"):
        resolve_agent_shell(platform_name="posix", executable="python", command_finder=_finder({"python": "/custom/python"}))


@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 可执行权限及符号链接")
def test_posix_lookup_uses_exact_child_path_and_rejects_nonexecutable_file(tmp_path: Path, monkeypatch) -> None:
    """实际 PATH 查找不能拾取宿主的同名命令；缺少执行权限须在启动前拒绝。"""
    selected = tmp_path / "child bin" / "bash"
    selected.parent.mkdir()
    selected.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "/host-only")
    with pytest.raises(FileNotFoundError):
        resolve_agent_shell(platform_name="posix", executable="bash", environment={"PATH": str(selected.parent)})
    selected.chmod(0o755)
    shell = resolve_agent_shell(platform_name="posix", executable="bash", environment={"PATH": str(selected.parent)})
    assert shell.executable == str(selected) and shell.login is False
    assert resolve_agent_shell(platform_name="posix", executable=str(selected), environment={"PATH": ""}).executable == str(selected)
    with pytest.raises(FileNotFoundError):
        resolve_agent_shell(platform_name="posix", executable="bash", environment={"PATH": ""})


@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 符号链接解释器")
def test_posix_named_alias_resolves_to_known_shell(tmp_path: Path) -> None:
    """明确的符号链接别名可按真实解释器指导模型，仍保留调用者选择的执行路径。"""
    alias = tmp_path / "chosen-shell"
    alias.symlink_to("/bin/sh")
    shell = resolve_agent_shell(platform_name="posix", executable=str(alias), environment={"PATH": ""})
    assert shell.kind in {"sh", "bash", "dash"} and shell.executable == str(alias)


@pytest.mark.skipif(os.name != "posix", reason="验证真实 POSIX 子进程 argv")
def test_real_bash_non_login_avoids_login_profile_and_preserves_argument_quoting(tmp_path: Path) -> None:
    """默认 bash 不读取登录 profile，变量、空格和分号只由已选择的解释器解析一次。"""
    bash = shutil.which("bash", path=os.defpath)
    if not bash:
        pytest.skip("系统没有 bash")
    profile_marker = tmp_path / "login-profile-read"
    (tmp_path / ".bash_profile").write_text(f"touch '{profile_marker}'\n", encoding="utf-8")
    environment = {"HOME": str(tmp_path), "PATH": os.defpath, "SHELL": bash, "TASK_VALUE": "space ; literal"}
    shell = resolve_agent_shell(platform_name="posix", environment=environment)
    result = subprocess.run(shell.build_argv('printf "%s" "$TASK_VALUE"; shopt -q login_shell && exit 9; exit 0'),
                            env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0 and result.stdout == "space ; literal"
    assert not profile_marker.exists()


def test_windows_explicit_interpreter_overrides_git_and_keeps_utf8() -> None:
    """Windows 显式选择 pwsh 优先于 Git，且保留本项目 UTF-8 初始化。"""
    shell = resolve_agent_shell(platform_name="nt", executable="pwsh", login=False, environment={},
                                command_finder=_finder({"git": "C:/Git/cmd/git.exe", "pwsh": "C:/PowerShell/pwsh.exe"}))
    assert shell.kind == "pwsh" and shell.login is False and shell.git_available is True
    assert shell.arguments == ("-NoLogo", "-NoProfile", "-Command") and "chcp.com" in shell.command_prefix


@pytest.mark.parametrize("name", ["pwsh", "powershell", "cmd"])
def test_windows_interpreters_reject_unsupported_login_mode(name: str) -> None:
    """Windows 命令宿主不能把 login=True 偷换成不读取 profile 的普通执行。"""
    finder = _finder({name: f"C:/commands/{name}.exe"})
    with pytest.raises(ValueError, match="不支持 login=True"):
        resolve_agent_shell(platform_name="nt", executable=name, login=True, command_finder=finder, environment={})
    assert resolve_agent_shell(platform_name="nt", executable=name, login=False, command_finder=finder).login is False


def test_windows_explicit_powershell_is_not_described_as_powershell_seven() -> None:
    """明确指定 Windows PowerShell 时，模型须知道真实语言宿主而非冒称 pwsh。"""
    shell = resolve_agent_shell(platform_name="nt", executable="powershell", command_finder=_finder({"powershell": "C:/Windows/powershell.exe"}))
    assert "Windows PowerShell" in shell.prompt_guidance() and "PowerShell 7" not in shell.prompt_guidance()


def test_windows_git_bash_preserves_legacy_default_but_honors_explicit_login() -> None:
    """未指定登录选项保持旧 argv，明确选项则真实启停 bash 登录模式。"""
    git = "C:/Git/cmd/git.exe"
    bash = "C:/Git/bin/bash.exe"
    finder = _finder({"git": git})
    with patch("app.agent.shell.Path.is_file", lambda candidate: str(candidate).replace("\\", "/") == bash):
        default = resolve_agent_shell(platform_name="nt", command_finder=finder, environment={}, cwd="C:/project")
        non_login = resolve_agent_shell(platform_name="nt", command_finder=finder, environment={}, login=False, cwd="C:/project")
        login = resolve_agent_shell(platform_name="nt", command_finder=finder, environment={}, login=True, cwd="C:/project")
    assert default.arguments == ("--noprofile", "--norc", "-lc") and default.login is True
    assert non_login.arguments == ("-c",) and non_login.login is False
    assert login.arguments == ("-lc",) and login.login is True


def test_windows_lookup_uses_provided_path_and_comspec(monkeypatch) -> None:
    """即使测试宿主不是 Windows，默认查找也必须使用传给子进程的 PATH/COMSPEC。"""
    paths = []

    def which(command: str, *, path: str):
        """记录待执行环境的查找路径，不读取本机安装情况。"""
        paths.append((command, path))
        return None

    monkeypatch.setattr("app.agent.shell.shutil.which", which)
    shell = resolve_agent_shell(platform_name="nt", environment={"PATH": "child-path", "COMSPEC": "C:/child/cmd.exe"}, cwd="C:/launch")
    assert paths and all(path == "C:\\launch\\child-path" for _, path in paths)
    assert shell.executable == "C:/child/cmd.exe" and shell.arguments == ("/d", "/s", "/c")


def test_cwd_resolves_root_relative_absolute_and_parent_paths(tmp_path: Path) -> None:
    """三个命令入口统一以 ROOT_PATH 为默认基准，仍允许明确指定根目录之外的位置。"""
    root = tmp_path / "project"
    child = root / "nested"
    child.mkdir(parents=True)
    assert resolve_agent_cwd(None, root_path=root) == str(root)
    assert resolve_agent_cwd("", root_path=root) == str(root)
    assert resolve_agent_cwd("nested", root_path=root) == str(child)
    assert resolve_agent_cwd(str(child), root_path=root) == str(child)
    assert resolve_agent_cwd("..", root_path=root) == str(tmp_path)


def test_cwd_expands_user_home_and_resolves_symlink(tmp_path: Path, monkeypatch) -> None:
    """用户目录与符号链接展开规则应与原 manager 一致，结果始终是存在的绝对目录。"""
    sample_home = tmp_path / "user"
    nested = sample_home / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(sample_home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(sample_home))
    assert resolve_agent_cwd("~/nested", root_path=tmp_path) == str(nested)
    if os.name == "posix":
        alias = tmp_path / "alias"
        alias.symlink_to(nested)
        assert resolve_agent_cwd("alias", root_path=tmp_path) == str(nested)


def test_cwd_rejects_missing_paths_files_and_missing_default_root(tmp_path: Path) -> None:
    """目录有效性校验包含默认 ROOT_PATH，避免启动后才得到含糊的子进程错误。"""
    filename = tmp_path / "file"
    filename.touch()
    with pytest.raises(FileNotFoundError, match="工作目录不存在"):
        resolve_agent_cwd("missing", root_path=tmp_path)
    with pytest.raises(FileNotFoundError, match="工作目录不存在"):
        resolve_agent_cwd(None, root_path=tmp_path / "missing-root")
    with pytest.raises(NotADirectoryError, match="工作目录不是目录"):
        resolve_agent_cwd("file", root_path=tmp_path)
    with pytest.raises(ValueError, match="cwd 必须"):
        resolve_agent_cwd(42, root_path=tmp_path)


def test_windows_comspec_cannot_silently_change_command_language() -> None:
    """COMSPEC 若指向另一种语言，不能继续附加 cmd 参数后宣称正确执行。"""
    with pytest.raises(ValueError, match="COMSPEC 必须指向 cmd.exe"):
        resolve_agent_shell(platform_name="nt", environment={"COMSPEC": "C:/bin/pwsh.exe"}, command_finder=_finder({}))


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_prompt_keeps_business_guidance_when_default_shell_is_invalid(error_type: type[Exception]) -> None:
    """默认解释器错误不能阻断业务对话，提示保留恢复入口且不泄露异常中的私有路径。"""
    private_path = "/private/account/secret-shell"
    with patch("app.agent.prompt.resolve_agent_shell", side_effect=error_type(private_path)):
        information = PromptManager()._get_moviepilot_info()
    assert "默认命令解释器不可用" in information and "显式选择可用的 `shell`" in information
    assert "当前日期" in information and "运行环境" in information and "query_doctor_report" in information
    assert "moviepilot_api" in information and "关键运行路径" in information
    assert private_path not in information


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX PATH 与符号链接定位")
@pytest.mark.parametrize("selection", ["relative-executable", "relative-path"])
def test_relative_shell_search_uses_launch_cwd_without_changing_parent(tmp_path: Path, monkeypatch, selection: str) -> None:
    """父目录和启动目录均有同名程序时，显式路径与 PATH 都必须选中启动目录的解释器。"""
    parent_dir, launch_dir = tmp_path / "parent", tmp_path / "launch"
    for folder in (parent_dir, launch_dir):
        (folder / "bin").mkdir(parents=True)
        (folder / "bin" / "bash").symlink_to("/bin/bash")
    monkeypatch.chdir(parent_dir)
    environment = {"PATH": "bin", "SHELL": "bash"}
    executable = "./bin/bash" if selection == "relative-executable" else None
    shell = resolve_agent_shell(platform_name="posix", executable=executable, cwd=str(launch_dir), environment=environment)
    assert shell.executable == str(launch_dir / "bin" / "bash") and shell.kind == "bash"
    assert Path.cwd() == parent_dir and environment == {"PATH": "bin", "SHELL": "bash"}
    result = subprocess.run(shell.build_argv("printf '%s' resolved-launch-shell"), cwd=launch_dir, env=environment,
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0 and result.stdout == "resolved-launch-shell"


@pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 空 PATH 项解析")
def test_empty_path_entry_searches_launch_cwd_and_preserves_parent(tmp_path: Path, monkeypatch) -> None:
    """PATH 空项表示当前目录，规范后应为子进程的当前目录而非宿主目录。"""
    parent_dir, launch_dir = tmp_path / "parent", tmp_path / "launch"
    parent_dir.mkdir()
    launch_dir.mkdir()
    (launch_dir / "bash").symlink_to("/bin/bash")
    monkeypatch.chdir(parent_dir)
    shell = resolve_agent_shell(platform_name="posix", executable="bash", cwd=str(launch_dir), environment={"PATH": ":missing"})
    assert shell.executable == str(launch_dir / "bash") and Path.cwd() == parent_dir


def test_windows_relative_path_entries_use_launch_drive_and_never_parent_lookup(monkeypatch) -> None:
    """Windows PATH 按分号、盘符和根路径解析，which 只接收明确绝对候选地址。"""
    calls = []
    expected_path = r"C:\launch\bin;D:\tools;C:\launch;C:\launch\local;C:\rooted"

    def which(command: str, *, path: str):
        """模拟 .exe 扩展解析，同时记录实际查找目录。"""
        calls.append((command, path))
        return r"C:\launch\bin\pwsh.exe" if command == r"C:\launch\bin\pwsh" else None

    monkeypatch.setattr("app.agent.shell.shutil.which", which)
    shell = resolve_agent_shell(platform_name="nt", executable="pwsh", cwd=r"C:\launch",
                                environment={"PATH": r"bin;D:\tools;;C:local;\rooted"})
    assert shell.executable == r"C:\launch\bin\pwsh.exe"
    assert calls and all(ntpath.isabs(command) and path == expected_path for command, path in calls)


@pytest.mark.parametrize(("executable", "expected"), [
    (r".\bin\pwsh.exe", r"C:\launch\bin\pwsh.exe"),
    (r"C:bin\pwsh.exe", r"C:\launch\bin\pwsh.exe"),
    (r"\tools\pwsh.exe", r"C:\tools\pwsh.exe"),
])
def test_windows_relative_executable_uses_launch_cwd_with_injected_finder(executable: str, expected: str) -> None:
    """注入 finder 仍只接收一个名称参数，但带目录的相对解释器已确定为实际启动目标。"""
    calls = []

    def finder(command: str):
        """仅暴露指定候选路径，防止回退到宿主隐式默认目录。"""
        calls.append(command)
        return command if command == expected else None

    shell = resolve_agent_shell(platform_name="nt", executable=executable, cwd=r"C:\launch",
                                environment={}, command_finder=finder)
    assert shell.executable == expected and expected in calls


def test_windows_cross_drive_relative_path_is_rejected() -> None:
    """另一盘符的隐藏当前目录无法从 launch cwd 推导，必须要求明确的绝对地址。"""
    with pytest.raises(ValueError, match="跨盘符"):
        resolve_agent_shell(platform_name="nt", executable=r"D:bin\pwsh.exe", cwd=r"C:\launch",
                            environment={}, command_finder=_finder({}))


def test_windows_relative_comspec_uses_launch_cwd() -> None:
    """显式相对 COMSPEC 不得在切换到另一目录后变成不同 cmd 入口。"""
    shell = resolve_agent_shell(platform_name="nt", cwd=r"C:\launch", command_finder=_finder({}),
                                environment={"COMSPEC": r".\tools\cmd.exe"})
    assert shell.executable == r"C:\launch\tools\cmd.exe"
