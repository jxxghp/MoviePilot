"""字节容量的解析与显示基础能力。"""

import bisect
import re
from typing import Union


def parse_size(text: Union[str, int, float]) -> int:
    """将带二进制容量单位的文本转换为字节数。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if text.isdigit():
        return int(text)
    normalized = text.replace(",", "").replace(" ", "").upper()
    size_text = re.sub(r"[KMGTPI]*B?", "", normalized, flags=re.IGNORECASE)
    try:
        size = float(size_text)
    except ValueError:
        return 0
    if "PB" in normalized or "PIB" in normalized:
        size *= 1024 ** 5
    elif "TB" in normalized or "TIB" in normalized:
        size *= 1024 ** 4
    elif "GB" in normalized or "GIB" in normalized:
        size *= 1024 ** 3
    elif "MB" in normalized or "MIB" in normalized:
        size *= 1024 ** 2
    elif "KB" in normalized or "KIB" in normalized:
        size *= 1024
    return round(size)


def format_compact_size(size: Union[str, float, int], precision: int = 2) -> str:
    """将字节数格式化为不带尾部 B 的紧凑容量描述。"""
    if size is None:
        return ""
    # 历史实现把 re.IGNORECASE 作为 count 位置参数传入；这里保留其最多替换两次、
    # 且仅匹配大写单位的实际行为，避免旧插件在边缘输入上发生变化。
    normalized = re.sub(r"\s|B|iB", "", str(size), count=re.IGNORECASE)
    if normalized.replace(".", "").isdigit():
        try:
            numeric_size = float(normalized)
            thresholds = [
                (1024 - 1, "K"),
                (1024 ** 2 - 1, "M"),
                (1024 ** 3 - 1, "G"),
                (1024 ** 4 - 1, "T"),
            ]
            index = bisect.bisect_left(
                [threshold for threshold, _unit in thresholds], numeric_size
            ) - 1
            if index == -1:
                return f"{numeric_size}B"
            threshold, unit = thresholds[index]
            return f"{round(numeric_size / (threshold + 1), precision)}{unit}"
        except ValueError:
            return ""
    if re.findall(r"[KMGTP]", normalized, re.IGNORECASE):
        return normalized
    return f"{normalized}B"


def format_size(size_bytes: int) -> str:
    """将字节数转换为带空格和完整单位的人类可读格式。"""
    if not size_bytes:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_bytes)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"
