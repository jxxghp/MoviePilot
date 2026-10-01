"""对 JSON 工具回执分类，为循环检测提供结果语义。"""

import json
from typing import Any

FILE_MUTATING_TOOL_NAMES = frozenset({'write_file', 'patch'})
GUARDRAIL_REFUSAL_KEY = 'guardrail_refusal'


def safe_json_loads(value: str) -> Any:
    """无法解析的自然语言结果返回 None，不能让检测异常中断真实工具。"""
    try:
        return json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None


def is_guardrail_refusal(result: Any) -> bool:
    """Return True when ``result`` (JSON string or parsed dict) is a harness refusal."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
    return isinstance(data, dict) and data.get(GUARDRAIL_REFUSAL_KEY) is True


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
