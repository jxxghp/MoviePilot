import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from app.agent.tools.impl.query_workflows import QueryWorkflowsTool
from app.application.workflow import WorkflowSnapshot


def _workflow() -> WorkflowSnapshot:
    """构造 Agent 查询使用的真实工作流快照。"""
    return WorkflowSnapshot(
        id=1,
        name="demo",
        description="demo workflow",
        timer=None,
        trigger_type="manual",
        event_type=None,
        event_conditions={},
        state="S",
        current_action=None,
        result="x" * 10000,
        run_count=1,
        actions=(),
        flows=(),
        context={},
        execution_config={},
        execution_state={},
        add_time="2026-05-08 10:00:00",
        last_time="2026-05-08 10:01:00",
    )


def test_query_workflows_omits_large_result_field(monkeypatch) -> None:
    """Agent 列表查询使用统一快照服务且不返回大结果字段。"""
    tool = QueryWorkflowsTool(session_id="session-1", user_id="10001")
    query = MagicMock()
    query.list = AsyncMock(return_value=[_workflow()])
    monkeypatch.setattr(
        "app.agent.tools.impl.query_workflows.get_configured_workflow_query",
        lambda: query,
    )

    result = asyncio.run(tool.run())

    payload = json.loads(result)
    assert len(payload) == 1
    assert payload[0]["name"] == "demo"
    assert "result" not in payload[0]
    query.list.assert_awaited_once_with()
