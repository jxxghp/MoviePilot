"""Agent 命令解释器选择与子进程编码策略。"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
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
    """描述 Agent 实际使用的解释器、登录模式及与其一致的命令参数。"""

    kind: str
    executable: str
    arguments: tuple[str, ...]
    command_prefix: str = ""
    git_available: bool = False
    login: bool = False

    def build_argv(self, command: str) -> list[str]:
        """生成无需再次经过系统默认 shell 的命令参数。"""
        command_text = f"{self.command_prefix}\n{command}" if self.command_prefix else command
        return [self.executable, *self.arguments, command_text]

    def prompt_guidance(self) -> str:
        """指导模型使用实际解释器语法，不泄露可执行文件的安装路径。"""
        selection_guidance = "可通过 `shell` 和 `login` 显式选择解释器及登录模式，命令语法须匹配本次选择。"
        if self.kind in _POSIX_SHELL_NAMES:
            name = _POSIX_SHELL_NAMES[self.kind]
            mode = "登录模式" if self.login else "非登录模式"
            return (f"- Agent 命令环境: 默认使用 {name}（{mode}）及 {name} 语法。"
                    f"{selection_guidance}`run`、后台 pipe 和 PTY 会话共享相同选择策略。")
        if self.kind == "git-bash":
            return (
                "- Windows Agent 命令环境: 默认使用 Git Bash（UTF-8）及 Git Bash/POSIX 语法；"
                f"涉及仓库、源码状态或版本控制时优先使用 `git`。{selection_guidance}"
            )
        if self.kind in {"pwsh", "powershell"}:
            name = "PowerShell 7 (`pwsh`, UTF-8)" if self.kind == "pwsh" else "Windows PowerShell (`powershell`, UTF-8)"
            git_guidance = "；仓库和版本控制操作仍优先使用 `git`" if self.git_available else ""
            return (
                f"- Windows Agent 命令环境: 默认使用 {name} 及 PowerShell 语法"
                f"{git_guidance}。{selection_guidance}"
            )
        git_guidance = "；仓库和版本控制操作优先使用 `git`" if self.git_available else ""
        return (
            "- Windows Agent 命令环境: 默认使用 cmd.exe（UTF-8）及 cmd 语法"
            f"{git_guidance}。{selection_guidance}"
        )


_POSIX_SHELL_NAMES = {
    "sh": "POSIX sh", "bash": "Bash", "zsh": "Zsh", "dash": "Dash", "ash": "Ash",
    "ksh": "KornShell", "mksh": "MirBSD KornShell", "fish": "Fish", "csh": "C shell", "tcsh": "Tcsh",
}


def _shell_kind(executable: str, *, windows: bool) -> str:
    """按启动名称识别语法，未知别名只有解析到已知解释器的符号链接才可使用。"""
    name = PureWindowsPath(executable).name.lower().removesuffix(".exe") if windows else Path(executable).name
    if name in _POSIX_SHELL_NAMES or (windows and name in {"pwsh", "powershell", "cmd"}):
        return name
    if not windows:
        resolved_name = Path(executable).resolve().name
        if resolved_name in _POSIX_SHELL_NAMES:
            return resolved_name
    raise ValueError(f"不支持的命令解释器: {name}")


def _find_executable(executable: str, finder: CommandFinder, *, windows: bool) -> str:
    """解析单个可执行文件，拒绝空值、命令行参数和不可执行路径后再启动进程。"""
    if not isinstance(executable, str) or not executable.strip() or "\x00" in executable:
        raise ValueError("shell 必须是单个已安装解释器的名称或路径")
    located = finder(str(Path(executable).expanduser()) if not windows else executable)
    if not located:
        raise FileNotFoundError(f"命令解释器不存在或不可执行: {executable}")
    return located


def _posix_policy(kind: str, executable: str, *, login: bool, git_available: bool = False) -> AgentShell:
    """POSIX 类解释器使用统一 exec 参数；C shell 不支持登录与命令执行组合。"""
    if login and kind in {"csh", "tcsh"}:
        raise ValueError(f"{kind} 不支持 login=True 与命令执行组合")
    arguments = ("-lc",) if login else ("-c",)
    return AgentShell(kind, executable, arguments, git_available=git_available, login=login)


def _windows_policy(kind: str, executable: str, *, login: Optional[bool], git_available: bool,
                    legacy_git_bash: bool = False) -> AgentShell:
    """保留 Windows 默认调用合同，显式登录只在支持登录 shell 的解释器上启用。"""
    if kind in _POSIX_SHELL_NAMES or kind == "git-bash":
        if legacy_git_bash and login is None:
            return AgentShell("git-bash", executable, ("--noprofile", "--norc", "-lc"), git_available=True, login=True)
        policy = _posix_policy("bash" if kind == "git-bash" else kind, executable,
                               login=bool(login), git_available=git_available)
        return AgentShell(kind, policy.executable, policy.arguments, git_available=git_available, login=policy.login)
    if login:
        raise ValueError(f"Windows {kind} 不支持 login=True；请使用非登录命令模式")
    if kind in {"pwsh", "powershell"}:
        return AgentShell(kind, executable, ("-NoLogo", "-NoProfile", "-Command"),
                          command_prefix=_POWERSHELL_UTF8_PREFIX, git_available=git_available)
    return AgentShell("cmd", executable, ("/d", "/s", "/c"), command_prefix="chcp 65001>nul &", git_available=git_available)


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


def _launch_path(value: str, cwd: str, *, windows: bool) -> str:
    """相对路径以即将启动的目录为基准，Windows 跨盘符相对路径必须显式补全。"""
    if windows:
        target, base = PureWindowsPath(value), PureWindowsPath(cwd)
        if target.drive and not target.root and target.drive.casefold() != base.drive.casefold():
            raise ValueError("跨盘符的相对 shell/PATH 路径需要改为绝对路径")
        return value if ntpath.isabs(value) else ntpath.normpath(ntpath.join(cwd, value))
    return value if posixpath.isabs(value) else posixpath.normpath(posixpath.join(cwd, value))


def _shell_finder(environment: Mapping[str, str], supplied: Optional[CommandFinder], *,
                  cwd: str, windows: bool) -> CommandFinder:
    """查找全部以 launch cwd 定位，Windows 裸命令也不借用父进程的隐式当前目录。"""
    paths = ntpath if windows else posixpath
    entries = [_launch_path(entry, cwd, windows=windows)
               for entry in environment.get("PATH", paths.defpath).split(paths.pathsep)]
    search_path = paths.pathsep.join(entries)

    def find(command: str) -> Optional[str]:
        """注入 finder 的一参数接口保持不变，明确的相对路径先转换成实际绝对目标。"""
        selected = _launch_path(command, cwd, windows=windows) if paths.dirname(command) else command
        if supplied is not None:
            located = supplied(selected)
        elif not windows or paths.dirname(selected):
            located = shutil.which(selected, path=search_path)
        else:
            located = next((found for entry in entries
                            if (found := shutil.which(paths.join(entry, selected), path=search_path))), None)
        if located is None:
            return None
        return _launch_path(located, cwd, windows=windows)

    return find


def resolve_agent_shell(
    *,
    platform_name: Optional[str] = None,
    command_finder: Optional[CommandFinder] = None,
    environment: Optional[Mapping[str, str]] = None,
    executable: Optional[str] = None,
    login: Optional[bool] = None,
    cwd: Optional[str] = None,
) -> AgentShell:
    """
    为一次性命令、后台 pipe 和 PTY 统一解析实际解释器及登录参数。

    :param platform_name: 用于测试覆盖的 os.name，默认读取当前平台
    :param command_finder: 可注入命令定位函数；默认按实际子进程 PATH 查找
    :param environment: 完整子进程环境；未传时继承宿主，POSIX 的 SHELL 未设置时再使用宿主 SHELL
    :param executable: 明确指定解释器名称或单个可执行路径，优先于平台默认策略
    :param login: 明确启停登录模式；POSIX 默认关闭，Windows 未指定时保留既有调用参数
    :param cwd: 启动目录，相对 shell 与 PATH 项都以此为基准；未传时使用父进程当前目录
    :return: 可以直接交给 exec 的解释器策略，未知语言或无效组合在启动前拒绝
    """
    if login is not None and not isinstance(login, bool):
        raise ValueError("login 必须为布尔值")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("shell cwd 必须是目录路径字符串")
    current_environment = environment if environment is not None else os.environ
    windows = (platform_name or os.name) == "nt"
    launch_cwd = _launch_path(cwd or os.getcwd(), os.getcwd(), windows=windows)
    finder = _shell_finder(current_environment, command_finder, cwd=launch_cwd, windows=windows)
    if not windows:
        selected = executable if executable is not None else current_environment.get("SHELL") or os.environ.get("SHELL") or "/bin/sh"
        path = _find_executable(selected, finder, windows=False)
        return _posix_policy(_shell_kind(path, windows=False), path, login=bool(login))

    git_path = finder("git") or finder("git.exe")
    if executable is not None:
        path = _find_executable(executable, finder, windows=True)
        return _windows_policy(_shell_kind(path, windows=True), path, login=login, git_available=bool(git_path))
    if git_path:
        bash_path = _find_git_bash(git_path, finder)
        if bash_path:
            return _windows_policy("git-bash", bash_path, login=login, git_available=True, legacy_git_bash=True)
    pwsh_path = finder("pwsh") or finder("pwsh.exe")
    if pwsh_path:
        return _windows_policy("pwsh", pwsh_path, login=login, git_available=bool(git_path))
    cmd_path = current_environment.get("COMSPEC") or finder("cmd") or finder("cmd.exe") or "cmd.exe"
    if ntpath.dirname(cmd_path):
        cmd_path = _launch_path(cmd_path, launch_cwd, windows=True)
    if _shell_kind(cmd_path, windows=True) != "cmd":
        raise ValueError("COMSPEC 必须指向 cmd.exe；其他解释器请通过 shell 明确指定")
    return _windows_policy("cmd", cmd_path, login=login, git_available=bool(git_path))


def resolve_agent_cwd(cwd: Optional[str], *, root_path: Path) -> str:
    """统一默认根目录、相对目录和用户目录展开，并在启动前验证实际目录存在。"""
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd 必须是目录路径字符串")
    path = Path(cwd).expanduser() if cwd else root_path.expanduser()
    if not path.is_absolute():
        path = root_path / path if cwd else path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"工作目录不存在: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"工作目录不是目录: {path}")
    return str(path)


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
