"""Agent 命令工具的安全校验逻辑。"""

from __future__ import annotations

import os.path
import re
import shlex


_SHELL_PUNCTUATION = ";&|<>"


COMMAND_FORBIDDEN_KEYWORDS = (
    ":(){ :|:& };:",
    "dd if=/dev/zero",
    "mkfs",
    "reboot",
    "shutdown",
)

COMMAND_DANGEROUS_PATTERNS = (
    re.compile(r"\brm\s+[^;&|]*-[^\s;&|]*[rR][fF]?[^\s;&|]*\s+/(?:\s|$|[;&|])"),
    re.compile(r"\bdd\s+[^;&|]*(?:of=/dev/(?:sd[a-z]\d*|nvme\d+n\d+p?\d*|disk\d+)|if=/dev/zero)"),
    re.compile(r"\b(?:mkfs|fdisk|parted|diskutil)\b"),
    re.compile(r"\b(?:chmod|chown)\s+[^;&|]*-R[^;&|]*\s+/(?:\s|$|[;&|])"),
    re.compile(r"\b(?:reboot|shutdown|halt|poweroff)\b"),
)


def _command_tokens(command: str) -> list[str]:
    """尽力解析 shell 命令 token，解析失败时退回空白分割。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return re.split(r"\s+", command.strip())


def _command_segments(command: str) -> list[list[str]]:
    """按 shell 控制运算符拆分命令，避免跨命令误关联参数。"""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in _command_tokens(command):
        if token and all(char in _SHELL_PUNCTUATION for char in token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _contains_recursive_root_delete(command: str) -> bool:
    """识别递归删除根目录或一级目录的 rm 命令。"""
    for tokens in _command_segments(command):
        rm_indexes = [
            index
            for index, token in enumerate(tokens)
            if token == "rm" or token.endswith("/rm")
        ]
        for rm_index in rm_indexes:
            rm_arguments = tokens[rm_index + 1 :]
            has_recursive = any(
                token.startswith("-") and ("r" in token or "R" in token)
                for token in rm_arguments
            )
            if not has_recursive:
                continue

            for token in rm_arguments:
                path_value = token.strip("\"'")
                if not path_value.startswith("/"):
                    continue
                norm_path = os.path.normpath(path_value)
                if norm_path == "/" or re.match(r"^/[^/]+$", norm_path):
                    return True
    return False


def detect_dangerous_command(command: str) -> str:
    """返回危险命令原因，安全时返回空字符串。"""
    normalized = str(command or "").strip()
    if not normalized:
        return "命令不能为空"
    for keyword in COMMAND_FORBIDDEN_KEYWORDS:
        if keyword in normalized:
            return f"命令包含禁止使用的关键字 '{keyword}'"
    if _contains_recursive_root_delete(normalized):
        return "命令疑似递归删除根目录或一级目录"
    for pattern in COMMAND_DANGEROUS_PATTERNS:
        if pattern.search(normalized):
            return "命令匹配高危系统操作模式"
    return ""


def validate_command_safety(command: str, *, confirmed: bool = False) -> None:
    """
    校验 shell 命令安全性。

    :param command: 待执行命令
    :param confirmed: 是否已经通过显式参数确认高危操作
    """
    reason = detect_dangerous_command(command)
    if not reason:
        return
    if confirmed and reason != "命令不能为空":
        return
    raise ValueError(f"{reason}。如确认需要执行，请设置 confirm_dangerous=true")
