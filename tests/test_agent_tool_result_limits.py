import asyncio
import json
import unittest

from app.agent.tools.base import (
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    MoviePilotTool,
    format_tool_result_for_agent,
)


class OversizedResultTool(MoviePilotTool):
    name: str = "oversized_result_tool"
    description: str = "Tool used to verify result truncation."

    async def run(self, **kwargs) -> str:
        return "x" * (DEFAULT_TOOL_RESULT_MAX_CHARS + 100)


class TestAgentToolResultLimits(unittest.TestCase):
    def test_arun_truncates_oversized_tool_result(self):
        tool = OversizedResultTool(session_id="session-1", user_id="10001")

        result = asyncio.run(tool._arun())
        payload = json.loads(result)

        self.assertTrue(payload["tool_result_truncated"])
        self.assertEqual(payload["tool_name"], "oversized_result_tool")
        self.assertEqual(payload["returned_chars"], DEFAULT_TOOL_RESULT_MAX_CHARS)
        self.assertGreater(payload["total_chars"], payload["returned_chars"])

    def test_formatter_preserves_sensitive_json_fields_for_agent_use(self):
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

        self.assertEqual(payload["cookie"], "uid=abc; token=secret")
        self.assertEqual(payload["nested"]["api_key"], "secret-key")
        self.assertEqual(payload["nested"]["plugin_author"], "MoviePilot")


if __name__ == "__main__":
    unittest.main()
