import asyncio
import json
import unittest
from unittest.mock import patch

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl._filter_rule_utils import parse_rule_string
from app.agent.tools.impl.query_builtin_filter_rules import (
    QueryBuiltinFilterRulesTool,
)


class TestAgentFilterRuleTools(unittest.TestCase):
    def test_factory_registers_filter_rule_tools(self):
        with patch(
            "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
            return_value=[],
        ):
            tools = MoviePilotToolFactory.create_tools(
                session_id="filter-rule-session",
                user_id="10001",
            )

        tool_names = {tool.name for tool in tools}
        expected = {
            "query_builtin_filter_rules",
            "query_custom_filter_rules",
            "query_rule_groups",
            "add_custom_filter_rule",
            "update_custom_filter_rule",
            "delete_custom_filter_rule",
            "add_rule_group",
            "update_rule_group",
            "delete_rule_group",
        }
        self.assertTrue(expected.issubset(tool_names))

    def test_query_builtin_filter_rules_returns_requested_rules(self):
        tool = QueryBuiltinFilterRulesTool(
            session_id="filter-rule-session",
            user_id="10001",
        )

        result = asyncio.run(tool.run(rule_ids=["BLU", "4K"]))
        payload = json.loads(result)

        self.assertTrue(payload["success"])
        self.assertEqual({"BLU", "4K"}, {item["id"] for item in payload["rules"]})
        self.assertEqual(">", payload["rule_string_syntax"]["level_separator"])

    def test_parse_rule_string_splits_priority_levels(self):
        parsed = parse_rule_string(
            "SPECSUB & CNVOI & 4K & !BLU > CNSUB & CNVOI & 4K & !BLU"
        )

        self.assertEqual(2, len(parsed["levels"]))
        self.assertEqual(
            ["SPECSUB", "CNVOI", "4K", "BLU", "CNSUB"],
            parsed["referenced_rules"],
        )
        self.assertEqual(
            "SPECSUB & CNVOI & 4K & !BLU",
            parsed["levels"][0]["expression"],
        )
