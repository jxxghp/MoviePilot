"""Agent 命令解释器选择与子进程编码策略。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

WINDOWS_UTF8_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}

_POWERSHELL_UTF8_PREFIX = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::InputEncoding = $utf8; "
    "[Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
    "$PSDefaultParameterValues['*:Encoding'] = 'utf8'; "
    "chcp.com 65001 | Out-Null;"
)

CommandFinder = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class AgentShell:
    """描述 Windows Agent 实际使用的命令解释器及模型指导。"""

    kind: str
    executable: str
    arguments: tuple[str, ...]
    command_prefix: str = ""
    git_available: bool = False

    def build_argv(self, command: str) -> list[str]:
        """生成无需再次经过系统默认 shell 的命令参数。"""
        command_text = f"{self.command_prefix}\n{command}" if self.command_prefix else command
        return [self.executable, *self.arguments, command_text]

    def prompt_guidance(self) -> str:
        """返回不包含可执行文件绝对路径的 Windows 命令指导。"""
        if self.kind == "git-bash":
            return (
                "- Windows Agent 命令环境: Git Bash（UTF-8）。所有 `execute_command` "
                "命令统一使用 Git Bash/POSIX 语法；涉及仓库、源码状态或版本控制时优先使用 "
                "`git`，不要混用 cmd.exe 或 PowerShell 语法。"
            )
        if self.kind == "pwsh":
            git_guidance = "；仓库和版本控制操作仍优先使用 `git`" if self.git_available else ""
            return (
                "- Windows Agent 命令环境: PowerShell 7 (`pwsh`, UTF-8)。所有 "
                f"`execute_command` 命令统一使用 PowerShell 语法{git_guidance}，不要混用 "
                "cmd.exe、Windows PowerShell 或 POSIX shell 语法。"
            )
        git_guidance = "；仓库和版本控制操作优先使用 `git`" if self.git_available else ""
        return (
            "- Windows Agent 命令环境: cmd.exe UTF-8 兼容回退"
            f"{git_guidance}。仅在 Git Bash 和 PowerShell 7 不可用时使用 cmd 语法。"
        )


def _find_git_bash(git_path: str, command_finder: CommandFinder) -> Optional[str]:
    """从 Git for Windows 常见布局中定位同一安装目录下的 bash.exe。"""
    git_executable = Path(git_path)
    git_root = git_executable.parent.parent
    candidates = (
        git_executable.parent / "bash.exe",
        git_root / "bin" / "bash.exe",
        git_root / "usr" / "bin" / "bash.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    bash_path = command_finder("bash") or command_finder("bash.exe")
    if not bash_path:
        return None
    bash_executable = Path(bash_path)
    if bash_executable.parent == git_executable.parent:
        return str(bash_executable)
    if bash_executable.parent.parent == git_root:
        return str(bash_executable)
    return None


def resolve_agent_shell(
    *,
    platform_name: Optional[str] = None,
    command_finder: Optional[CommandFinder] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[AgentShell]:
    """
    解析 Windows Agent 命令解释器，非 Windows 返回 None 以保留原有 shell 行为。

    :param platform_name: 用于测试覆盖的 os.name，默认读取当前平台
    :param command_finder: 命令定位函数，默认使用 shutil.which
    :param environment: 用于读取 COMSPEC 的环境变量
    :return: Windows 命令解释器策略，非 Windows 返回 None
    """
    if (platform_name or os.name) != "nt":
        return None

    finder = command_finder or shutil.which
    current_environment = environment if environment is not None else os.environ
    git_path = finder("git") or finder("git.exe")
    if git_path:
        bash_path = _find_git_bash(git_path, finder)
        if bash_path:
            return AgentShell(
                kind="git-bash",
                executable=bash_path,
                arguments=("--noprofile", "--norc", "-lc"),
                git_available=True,
            )

    pwsh_path = finder("pwsh") or finder("pwsh.exe")
    if pwsh_path:
        return AgentShell(
            kind="pwsh",
            executable=pwsh_path,
            arguments=("-NoLogo", "-NoProfile", "-Command"),
            command_prefix=_POWERSHELL_UTF8_PREFIX,
            git_available=bool(git_path),
        )

    cmd_path = (
        current_environment.get("COMSPEC")
        or finder("cmd")
        or finder("cmd.exe")
        or "cmd.exe"
    )
    return AgentShell(
        kind="cmd",
        executable=cmd_path,
        arguments=("/d", "/s", "/c"),
        command_prefix="chcp 65001>nul &",
        git_available=bool(git_path),
    )


def build_agent_subprocess_env(
    overrides: Optional[Mapping[str, Any]] = None,
    *,
    platform_name: Optional[str] = None,
) -> dict[str, str]:
    """
    构造 Agent 子进程环境，并在 Windows 上强制覆盖为 UTF-8 编解码。

    :param overrides: 调用方附加的环境变量
    :param platform_name: 用于测试覆盖的 os.name，默认读取当前平台
    :return: 可直接传给 subprocess 的字符串环境变量
    """
    environment = os.environ.copy()
    for key, value in (overrides or {}).items():
        if value is not None:
            environment[str(key)] = str(value)
    if (platform_name or os.name) == "nt":
        # Python 会把该变量的任意非空值视为启用，必须移除而不是写入 "0"。
        environment.pop("PYTHONLEGACYWINDOWSSTDIO", None)
        environment.update(WINDOWS_UTF8_ENVIRONMENT)
    return environment


def agent_text_subprocess_kwargs(
    *, platform_name: Optional[str] = None
) -> dict[str, Any]:
    """为 Windows Agent 文本子进程补充 UTF-8 解码参数，POSIX 返回空参数。"""
    if (platform_name or os.name) != "nt":
        return {}
    return {
        "encoding": "utf-8",
        "errors": "replace",
        "env": build_agent_subprocess_env(platform_name="nt"),
    }
