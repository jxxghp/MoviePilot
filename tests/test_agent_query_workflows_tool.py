import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.tools.impl.query_workflows import QueryWorkflowsTool


class TestQueryWorkflowsTool(unittest.TestCase):
    def test_query_workflows_omits_large_result_field(self):
        tool = QueryWorkflowsTool(session_id="session-1", user_id="10001")
        workflow = SimpleNamespace(
            id=1,
            name="demo",
            description="demo workflow",
            state="S",
            trigger_type="manual",
            run_count=1,
            timer=None,
            event_type=None,
            add_time="2026-05-08 10:00:00",
            last_time="2026-05-08 10:01:00",
            current_action=None,
            result="x" * 10000,
        )
        workflow_oper = MagicMock()
        workflow_oper.async_list = AsyncMock(return_value=[workflow])

        with patch(
            "app.agent.tools.impl.query_workflows.get_agent_workflow_port",
            return_value=workflow_oper,
        ):
            result = asyncio.run(tool.run())

        payload = json.loads(result)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "demo")
        self.assertNotIn("result", payload[0])
