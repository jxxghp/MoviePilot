"""
外部 MCP tools/list 的 schema 预算投影（compact opt-in）测试。
"""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import pytest
from starlette.requests import Request

from app.agent.policy.api import API_OPERATION_ROUTES
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent.tools.projection import (
    PROJECTION_MARKER,
    project_schema_for_budget,
    project_tool_schema,
    schema_within_client_budget,
    serialized_schema_bytes,
)
from app.api.endpoints import mcp


class OversizedSchemaTool(MoviePilotTool):
    """
    测试用超大 MCP schema 工具。
    """

    name: str = "oversized_schema_tool"
    description: str = "测试用超大 MCP schema 工具"

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """
        返回超出客户端压缩阈值的根级 oneOf 合同。
        """
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
        """
        返回固定测试结果。
        """
        return "ok"


def _receive_with_body(body: bytes) -> Callable[[], Awaitable[dict[str, Any]]]:
    """
    构造只投递一次请求体的 receive 通道。
    """

    async def receive() -> dict[str, Any]:
        """
        向测试请求提供预设的完整请求体。
        """
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _request(headers: Optional[list[tuple[bytes, bytes]]] = None, body: bytes = b"") -> Request:
    """
    构造测试用 HTTP 请求。
    """
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mcp",
            "headers": headers or [],
            "query_string": b"",
        },
        _receive_with_body(body),
    )


def _manager_with(tool: MoviePilotTool) -> MoviePilotToolsManager:
    """
    构造只包含指定工具的隔离 MCP 工具管理器。
    """
    manager = MoviePilotToolsManager(session_id="schema-budget-test", user_id="api_user")
    manager.tools = [tool]
    return manager


def _api_schema() -> dict[str, Any]:
    """
    读取运行时生成的 moviepilot_api 完整输入合同。
    """
    schema_path = Path(__file__).parents[1] / "app/agent/policy/resources/api_mcp_schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_oversized_schema_is_projected_to_object_contract() -> None:
    """
    超出阈值的 schema 必须投影为可被严格客户端接受的顶层对象合同。
    """
    schema = OversizedSchemaTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    assert not schema_within_client_budget(schema)

    projected, changed = project_tool_schema(schema)

    assert changed is True
    assert projected["type"] == "object"
    assert "oneOf" not in projected
    assert projected["required"] == ["action"]
    assert projected["properties"]["action"]["enum"] == schema["properties"]["action"]["enum"]
    assert projected[PROJECTION_MARKER]["mode"] == "compact"
    assert serialized_schema_bytes(projected) < serialized_schema_bytes(schema)


def test_schema_within_budget_is_returned_untouched() -> None:
    """
    阈值内的 schema 不得被投影，保持既有完整合同。
    """
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    assert schema_within_client_budget(schema)

    projected, changed = project_tool_schema(schema)

    assert changed is False
    assert projected is schema


def test_moviepilot_api_projection_keeps_discovery_and_cleans_branch_descriptions() -> None:
    """
    moviepilot_api 投影后保留 operation 枚举，且不再残留指向已删除分支的说明。
    """
    schema = _api_schema()
    assert not schema_within_client_budget(schema)

    projected = project_schema_for_budget(schema)

    assert projected["type"] == "object"
    for keyword in ("oneOf", "anyOf", "allOf", "$defs"):
        assert keyword not in projected
    properties = projected["properties"]
    assert set(properties["operation_id"]["enum"]) == set(schema["properties"]["operation_id"]["enum"])
    assert {"path_params", "query", "body"}.issubset(properties)
    assert "oneOf" not in projected["description"]
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "oneOf" not in serialized
    assert "$ref" not in serialized
    assert serialized_schema_bytes(projected) < serialized_schema_bytes(schema) // 20


def test_projection_does_not_leave_dangling_definition_refs() -> None:
    """
    定义表和组合分支随投影删除后，指向它们的本地引用必须一并去掉，只保留兄弟信息。
    """
    schema = {
        "type": "object",
        "properties": {
            "payload": {"$ref": "#/$defs/Payload", "description": "自由格式业务参数"},
            "branch": {"$dynamicRef": "#/oneOf/0/properties/action"},
            "legacy": {"$ref": "#/definitions"},
            "keep": {"$ref": "#/properties/kept"},
            "kept": {"type": "string"},
        },
        "required": ["payload"],
        "$defs": {"Payload": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "oneOf": [{"required": ["payload"]}],
        "description": "选择 oneOf 分支",
    }

    projected = project_schema_for_budget(schema)

    assert "$defs" not in projected
    assert "oneOf" not in projected
    assert projected["properties"]["payload"] == {"description": "自由格式业务参数"}
    assert projected["properties"]["branch"] == {}
    assert projected["properties"]["legacy"] == {}
    assert projected["properties"]["keep"] == {"$ref": "#/properties/kept"}
    assert projected["properties"]["kept"] == {"type": "string"}


def test_projection_adds_missing_root_object_type() -> None:
    """
    根节点缺少 ``type`` 时补 ``type: object``，已声明的根类型不被改写。
    """
    without_type = {"properties": {"payload": {"type": "string"}}}
    projected = project_schema_for_budget(without_type)
    assert projected["type"] == "object"
    assert projected["properties"] == {"payload": {"type": "string"}}

    declared_array = {"type": "array", "items": {"type": "string"}}
    assert project_schema_for_budget(declared_array)["type"] == "array"


@pytest.mark.parametrize("keyword", ["patternProperties", "dependentSchemas", "dependencies"])
def test_projection_cleans_nested_schema_maps_without_changing_field_names(keyword: str) -> None:
    """
    schema 映射中的分支要清理，但同名业务字段与默认值必须原样保留。
    """
    schema = {
        "type": "object",
        keyword: {"oneOf": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "properties": {"oneOf": {"type": "string"}},
        "default": {"oneOf": ["business value"]},
        "enum": [{"$defs": {"payload": 1}}],
    }
    original = deepcopy(schema)

    projected = project_schema_for_budget(schema)

    assert projected[keyword] == {"oneOf": {}}
    assert projected["properties"] == schema["properties"]
    assert projected["default"] == schema["default"]
    assert projected["enum"] == schema["enum"]
    assert schema == original


def test_projection_removes_nested_and_escaped_definition_refs() -> None:
    """
    清理嵌套定义的前向引用，并正确区分 JSON Pointer 转义与合法同名属性。
    """
    schema = {
        "type": "object",
        "properties": {
            "reference": {"$ref": "#/properties/a~1b~0c/$defs/Text", "description": "业务参数"},
            "encoded": {"$dynamicRef": "#/properties/a~1b~0c/%24defs/Text"},
            "kept": {"$ref": "#/properties/a~1b~0c/properties/oneOf"},
            "a/b~c": {
                "$defs": {"Text": {"type": "string"}},
                "properties": {"oneOf": {"type": "string"}},
            },
        },
    }

    projected = project_schema_for_budget(schema)

    assert projected["properties"]["reference"] == {"description": "业务参数"}
    assert projected["properties"]["encoded"] == {}
    assert projected["properties"]["kept"] == schema["properties"]["kept"]
    assert "$defs" not in projected["properties"]["a/b~c"]


@pytest.mark.parametrize("keyword", ["prefixItems", "items", "additionalItems", "additionalProperties"])
def test_projection_cleans_compositions_in_subschemas(keyword: str) -> None:
    """
    数组元素及附加属性子 schema 中的组合关键字也不能残留。
    """
    child = {"allOf": [{"type": "string"}]}
    schema = {"type": "object", keyword: [child] if keyword == "prefixItems" else child}

    projected = project_schema_for_budget(schema)

    assert projected[keyword] == ([{}] if keyword == "prefixItems" else {})


def test_handle_tools_list_keeps_full_moviepilot_api_one_of_by_default(monkeypatch) -> None:
    """
    handle_tools_list 默认返回完整 oneOf；compact 模式才投影。
    """
    manager = _manager_with(MoviePilotApiTool(session_id="session", user_id="api_user"))
    monkeypatch.setattr(mcp, "moviepilot_tool_manager", manager)

    tools = asyncio.run(mcp.handle_tools_list())["tools"]
    api_tool = next(tool for tool in tools if tool["name"] == "moviepilot_api")
    branches = api_tool["inputSchema"]["oneOf"]
    operation_ids = {branch["properties"]["operation_id"]["const"] for branch in branches}
    assert operation_ids == set(API_OPERATION_ROUTES)

    compacted = asyncio.run(mcp.handle_tools_list(mcp.MCP_SCHEMA_MODE_COMPACT))["tools"]
    compacted_api = next(tool for tool in compacted if tool["name"] == "moviepilot_api")
    assert "oneOf" not in compacted_api["inputSchema"]
    assert set(compacted_api["inputSchema"]["properties"]["operation_id"]["enum"]) == set(API_OPERATION_ROUTES)


def test_tools_list_compact_mode_projects_oversized_tools(monkeypatch) -> None:
    """
    compact 模式通过 handle_tools_list 投影超大 schema，默认模式完整返回。
    """
    manager = _manager_with(OversizedSchemaTool(session_id="session", user_id="api_user"))
    monkeypatch.setattr(mcp, "moviepilot_tool_manager", manager)

    listed = asyncio.run(mcp.handle_tools_list())["tools"]
    assert [tool["name"] for tool in listed] == ["oversized_schema_tool"]
    assert "oneOf" in listed[0]["inputSchema"]
    assert PROJECTION_MARKER not in listed[0]["inputSchema"]

    compacted = asyncio.run(mcp.handle_tools_list(mcp.MCP_SCHEMA_MODE_COMPACT))["tools"]
    assert "oneOf" not in compacted[0]["inputSchema"]
    assert PROJECTION_MARKER in compacted[0]["inputSchema"]


def test_jsonrpc_tools_list_honours_compact_header(monkeypatch) -> None:
    """
    JSON-RPC tools/list 仅在显式 compact 请求头下投影，未知取值与其它方法不受影响。
    """
    manager = _manager_with(OversizedSchemaTool(session_id="session", user_id="api_user"))
    monkeypatch.setattr(mcp, "moviepilot_tool_manager", manager)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")

    default_response = asyncio.run(mcp.mcp_jsonrpc(_request(body=body)))
    default_tools = json.loads(bytes(default_response.body).decode("utf-8"))["result"]["tools"]
    assert "oneOf" in default_tools[0]["inputSchema"]

    compact_response = asyncio.run(
        mcp.mcp_jsonrpc(_request(headers=[(b"x-mcp-schema-mode", b"compact")], body=body))
    )
    compact_tools = json.loads(bytes(compact_response.body).decode("utf-8"))["result"]["tools"]
    assert "oneOf" not in compact_tools[0]["inputSchema"]
    assert PROJECTION_MARKER in compact_tools[0]["inputSchema"]

    unknown_response = asyncio.run(
        mcp.mcp_jsonrpc(_request(headers=[(b"x-mcp-schema-mode", b"unknown")], body=body))
    )
    unknown_tools = json.loads(bytes(unknown_response.body).decode("utf-8"))["result"]["tools"]
    assert "oneOf" in unknown_tools[0]["inputSchema"]

    initialize_body = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
    ).encode("utf-8")
    initialize_response = asyncio.run(
        mcp.mcp_jsonrpc(
            _request(headers=[(b"x-mcp-schema-mode", b"compact")], body=initialize_body)
        )
    )
    assert "protocolVersion" in json.loads(bytes(initialize_response.body).decode("utf-8"))["result"]

    call_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_skill", "arguments": {}},
        }
    ).encode("utf-8")
    call_results = [
        json.loads(
            bytes(
                asyncio.run(
                    mcp.mcp_jsonrpc(_request(headers=headers, body=call_body))
                ).body
            ).decode("utf-8")
        )["result"]
        for headers in (None, [(b"x-mcp-schema-mode", b"compact")])
    ]
    assert call_results[0] == call_results[1]
    assert call_results[0]["isError"] is True


def test_rest_tool_list_always_returns_full_contract(monkeypatch) -> None:
    """
    三个 REST 工具端点不受客户端 schema 模式影响，始终返回完整合同。
    """
    manager = _manager_with(OversizedSchemaTool(session_id="session", user_id="api_user"))
    monkeypatch.setattr(mcp, "moviepilot_tool_manager", manager)

    tools = asyncio.run(mcp.list_tools(None))

    assert "oneOf" in tools[0]["inputSchema"]
    assert PROJECTION_MARKER not in tools[0]["inputSchema"]

    info = asyncio.run(mcp.get_tool_info("oversized_schema_tool", None))
    assert "oneOf" in info["inputSchema"]
    assert PROJECTION_MARKER not in info["inputSchema"]

    schema = asyncio.run(mcp.get_tool_schema("oversized_schema_tool", None))
    assert "oneOf" in schema
    assert PROJECTION_MARKER not in schema


def test_resolve_schema_mode_defaults_to_full() -> None:
    """
    schema 模式读取请求头，未知或缺失取值回落到 full。
    """
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b"compact")])) == "compact"
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b" COMPACT ")])) == "compact"
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b"full")])) == "full"
    assert mcp.resolve_schema_mode(_request([(b"x-mcp-schema-mode", b"unknown")])) == "full"
    assert mcp.resolve_schema_mode(_request()) == "full"
