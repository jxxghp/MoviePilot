"""
验证工具结果的大小限制、转义预览与数据保留。
"""

import asyncio
import json

from pydantic import Field

from app.agent.tools.base import (
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    MoviePilotTool,
    format_tool_result_for_agent,
)


class OversizedResultTool(MoviePilotTool):
    """
    返回超大文本以验证工具结果截断边界。
    """

    name: str = Field(default="oversized_result_tool", description="验证超限截断的固定工具名称")
    description: str = Field(default="用于验证结果截断的测试工具。", description="供模型理解测试工具用途的说明")

    async def run(self, **kwargs) -> str:
        """
        返回超过默认结果上限的固定文本，触发执行入口截断。
        """
        return "x" * (DEFAULT_TOOL_RESULT_MAX_CHARS + 100)


def test_arun_truncates_oversized_tool_result() -> None:
    """
    工具执行入口截断超限结果并保留完整长度信息。
    """
    tool = OversizedResultTool(session_id="session-1", user_id="10001")

    result = asyncio.run(tool._arun())
    payload = json.loads(result)

    assert payload["tool_result_truncated"]
    assert payload["tool_name"] == "oversized_result_tool"
    assert len(result) <= DEFAULT_TOOL_RESULT_MAX_CHARS
    assert payload["returned_chars"] == len(payload["content_preview"])
    assert payload["returned_chars"] < DEFAULT_TOOL_RESULT_MAX_CHARS
    assert payload["total_chars"] > payload["returned_chars"]


def test_formatter_keeps_escaped_preview_within_hard_limit() -> None:
    """
    大量换行转义后，最终工具结果仍不得超过配置的字符上限。
    """
    result = format_tool_result_for_agent(
        "line\n" * DEFAULT_TOOL_RESULT_MAX_CHARS,
        tool_name="escaped_result_tool",
    )
    payload = json.loads(result)

    assert len(result) <= DEFAULT_TOOL_RESULT_MAX_CHARS
    assert payload["tool_result_truncated"]
    assert payload["returned_chars"] == len(payload["content_preview"])


def test_formatter_preserves_sensitive_json_fields_for_agent_use() -> None:
    """
    结果格式化保留供已授权智能体使用的敏感 JSON 字段。
    """
    result = format_tool_result_for_agent(
        {
            "cookie": "uid=abc; token=secret",
            "nested": {
                "api_key": "secret-key",
                "plugin_author": "MoviePilot",
            },
        },
        tool_name="sensitive_tool",
    )
    payload = json.loads(result)

    assert payload["cookie"] == "uid=abc; token=secret"
    assert payload["nested"]["api_key"] == "secret-key"
    assert payload["nested"]["plugin_author"] == "MoviePilot"
