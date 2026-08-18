"""把外部取值拼接到基准目录前的安全校验。"""

from __future__ import annotations

from pathlib import PureWindowsPath

__all__ = [
    "ensure_path_segment",
    "is_safe_path_segment",
]

# 目录名中不允许出现的控制字符
_NULL_CHARACTER = "\x00"
# 指向当前目录或上级目录的保留名
_RESERVED_SEGMENTS = frozenset({".", ".."})


def is_safe_path_segment(value: str) -> bool:
    """判断取值能否安全地作为单层目录名拼接到基准目录下。

    按 Windows 规则解析取值，使 POSIX 与 Windows 两套路径分隔符、盘符和 UNC 根
    都被识别，从而在任意宿主平台上给出一致结论。

    :param value: 待判断的取值
    :return: 取值是不含分隔符、盘符且不指向上级目录的单层目录名时为 True
    """
    if not isinstance(value, str) or not value:
        return False
    if _NULL_CHARACTER in value or value in _RESERVED_SEGMENTS:
        return False
    candidate = PureWindowsPath(value)
    if candidate.drive or candidate.root:
        return False
    return len(candidate.parts) == 1


def ensure_path_segment(value: str, *, subject: str) -> str:
    """校验取值可安全用作单层目录名，并原样返回。

    :param value: 待校验的取值
    :param subject: 取值的含义，用于组装错误信息
    :return: 校验通过的取值
    :raises ValueError: 取值为空，或包含路径分隔符、盘符、空字符，或指向上级目录
    """
    if not is_safe_path_segment(value):
        raise ValueError(f"非法的{subject}：{value!r}")
    return value
