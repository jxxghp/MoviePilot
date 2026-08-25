"""历史版本比较规则。"""

import re
from typing import Optional, Tuple

__all__ = ["compare_version"]


_VERSION_LABELS = {"stable": -1, "rc": -2, "beta": -3, "alpha": -4}
_UNKNOWN_VERSION_LABEL = -5
_COMPARISON_TYPES = {"ge", "gt", "le", "lt", "eq", "==", ">=", ">", "<=", "<"}


def _normalize_version(version: str) -> list[int]:
    """将历史版本格式转换为保持原有排序语义的整数序列。"""
    parts = re.split(r"[.-]", version.strip().lstrip("vV"))
    return [
        int(part)
        if part.isdigit()
        else _VERSION_LABELS.get(part, _UNKNOWN_VERSION_LABEL)
        for part in parts
    ]


def compare_version(
    source: str,
    comparison: str,
    target: str,
    verbose: bool = False,
) -> Optional[bool] | Tuple[Optional[bool], str | Exception]:
    """按 MoviePilot 历史规则比较版本号，并可返回可读比较结果。"""
    try:
        if not source or not target:
            raise ValueError("要比较的版本号不全")
        if not comparison:
            raise ValueError("缺少比对模式，无法比对")
        if comparison not in _COMPARISON_TYPES:
            raise ValueError(f"设置的版本比对模式 {comparison} 不是有效的模式！")

        source_parts = _normalize_version(source)
        target_parts = _normalize_version(target)
        max_length = max(len(source_parts), len(target_parts))
        source_parts += [0] * (max_length - len(source_parts))
        target_parts += [0] * (max_length - len(target_parts))

        relation = "等于"
        for source_value, target_value in zip(source_parts, target_parts):
            if source_value > target_value:
                relation = "大于"
                break
            if source_value < target_value:
                relation = "小于"
                break

        matched = {
            "eq": relation == "等于",
            "==": relation == "等于",
            "ge": relation in {"大于", "等于"},
            ">=": relation in {"大于", "等于"},
            "gt": relation == "大于",
            ">": relation == "大于",
            "le": relation in {"小于", "等于"},
            "<=": relation in {"小于", "等于"},
            "lt": relation == "小于",
            "<": relation == "小于",
        }[comparison]
        display_relation = (
            "不等于" if comparison in {"eq", "=="} and not matched else relation
        )
        message = f"版本号 {source} {display_relation} 目标版本号 {target} ！"
        return (matched, message) if verbose else matched
    except Exception as err:
        return (None, err) if verbose else None
