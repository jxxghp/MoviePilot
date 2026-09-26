"""外部 MCP tools/list 的 schema 体积预算测试。"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.requests import Request

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent.tools.schema_budget import (
    MCP_TOOL_SCHEMA_BUDGET_BYTES,
    PROJECTION_MARKER,
    project_schema_for_budget,
    project_tool_schema,
    schema_within_budget,
    serialized_schema_bytes,
)
from app.api.endpoints import mcp


class OversizedSchemaTool(MoviePilotTool):
    """测试用超大 MCP schema 工具。"""

    name: str = "oversized_schema_tool"
    description: str = "测试用超大 MCP schema 工具"

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """返回超出预算的根级 oneOf 合同。"""
        actions = [f"action.{index}" for index in range(40)]
        return {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": actions}},
            "required": ["action"],
            "oneOf": [
                {
                    "type": "object",
                    "title": action,
                    "description": "分支说明" * 12,
                    "properties": {"action": {"const": action}, "arguments": {"type": "object"}},
                    "required": ["action"],
                }
                for action in actions
            ],
        }

    async def run(self, **kwargs) -> str:
        """返回固定测试结果。"""
        return "ok"


def _request(headers: list[tuple[bytes, bytes]] | None = None, query: str = "") -> Request:
    """构造测试用 HTTP 请求。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mcp",
            "headers": headers or [],
            "query_string": query.encode("utf-8"),
        }
    )


def test_oversized_schema_is_projected_to_object_contract() -> None:
    """超出预算的 schema 必须投影为可被严格客户端接受的顶层对象合同。"""
    schema = OversizedSchemaTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    assert not schema_within_budget(schema)

    projected, changed = project_tool_schema(schema)

    assert changed is True
    assert projected["type"] == "object"
    assert "oneOf" not in projected
    assert projected["required"] == ["action"]
    assert projected["properties"]["action"]["enum"] == schema["properties"]["action"]["enum"]
    assert projected[PROJECTION_MARKER]["budget_bytes"] == MCP_TOOL_SCHEMA_BUDGET_BYTES
    assert serialized_schema_bytes(projected) < serialized_schema_bytes(schema)


def test_schema_within_budget_is_returned_untouched() -> None:
    """预算内 schema 不得被投影，保持既有完整合同。"""
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    assert schema_within_budget(schema)

    projected, changed = project_tool_schema(schema)

    assert changed is False
    assert projected is schema


def test_moviepilot_api_schema_projection_keeps_operation_discovery() -> None:
    """moviepilot_api 投影后必须保留 operation 枚举与调用所需顶层字段。"""
    schema_path = Path(__file__).parents[1] / "app/agent/policy/resources/api_mcp_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert not schema_within_budget(schema)

    projected = project_schema_for_budget(schema)

    assert projected["type"] == "object"
    assert "oneOf" not in projected
    assert "$defs" not in projected
    assert projected["properties"]["operation_id"]["type"] == "string"
    assert len(projected["properties"]["operation_id"]["enum"]) == len(schema["properties"]["operation_id"]["enum"])
    assert {"path_params", "query", "body"}.issubset(projected["properties"])
    assert serialized_schema_bytes(projected) < serialized_schema_bytes(schema) // 20


def test_tools_list_projects_oversized_schemas_by_default(monkeypatch) -> None:
    """MCP tools/list 默认投影超大 schema，显式 full 模式仍返回完整合同。"""
    with (
        patch.object(MoviePilotToolFactory, "_get_builtin_tool_classes", return_value=[]),
        patch.object(MoviePilotToolFactory, "EXTERNAL_SERVICE_TOOL_CLASSES", ()),
    ):
        tool_manager = MoviePilotToolsManager(session_id="schema-budget-test", user_id="api_user")
        tool_manager.tools = [OversizedSchemaTool(session_id="schema-budget-test", user_id="api_user")]
        monkeypatch.setattr(mcp, "moviepilot_tool_manager", tool_manager)

        listed = asyncio.run(mcp.handle_tools_list())["tools"]
        assert [tool["name"] for tool in listed] == ["oversized_schema_tool"]
        assert listed[0]["inputSchema"]["type"] == "object"
        assert "oneOf" not in listed[0]["inputSchema"]
        assert PROJECTION_MARKER in listed[0]["inputSchema"]

        full_listed = asyncio.run(mcp.handle_tools_list(mcp.MCP_SCHEMA_MODE_FULL))["tools"]
        assert "oneOf" in full_listed[0]["inputSchema"]


def test_resolve_schema_mode_reads_header_query_and_falls_back() -> None:
    """schema 模式支持请求头与查询参数，未知取值回落 auto。"""
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b"full")])) == "full"
    assert mcp.resolve_schema_mode(_request(query="schema_mode=full")) == "full"
    assert mcp.resolve_schema_mode(_request()) == "auto"
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b"unknown")])) == "auto"
