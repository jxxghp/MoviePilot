"""下载器与媒体服务器外部 MCP 工具合同测试。"""

import asyncio
import json
from unittest.mock import patch

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.service_operation import (
    DownloaderOperationTool,
    MediaServerOperationTool,
)
from app.agent.tools.manager import MoviePilotToolsManager


def _branch(schema: dict, action: str) -> dict:
    """从 MCP oneOf schema 中读取指定 action 分支。"""
    return next(
        branch
        for branch in schema["oneOf"]
        if branch["properties"]["action"].get("const") == action
    )


def test_downloader_mcp_schema_exposes_every_action_argument_and_rule() -> None:
    """外部 MCP Client 应在 tools/list 中直接看到下载器条件参数合同。"""
    tool = DownloaderOperationTool(session_id="session", user_id="api_user")
    schema = tool.get_mcp_input_schema()
    branch = _branch(schema, "tasks.queue.move")
    arguments = branch["properties"]["arguments"]

    assert len(schema["oneOf"]) == len(schema["properties"]["action"]["enum"])
    assert arguments["properties"]["task_ids"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "多个任务的 provider 原生 hash 或 ID；与 task_id 二选一。",
        "minItems": 1,
    }
    assert arguments["properties"]["position"]["enum"] == ["top", "up", "down", "bottom"]
    assert arguments["required"] == ["position"]
    assert arguments["oneOf"] == [
        {"required": ["task_id"], "not": {"required": ["task_ids"]}},
        {"required": ["task_ids"], "not": {"required": ["task_id"]}},
    ]
    assert arguments["additionalProperties"] is False


def test_mediaserver_mcp_schema_exposes_nested_refresh_item_fields() -> None:
    """外部 MCP Client 应直接看到 metadata.refresh 的嵌套字段与枚举。"""
    tool = MediaServerOperationTool(session_id="session", user_id="api_user")
    schema = tool.get_mcp_input_schema()
    branch = _branch(schema, "metadata.refresh")
    arguments = branch["properties"]["arguments"]
    item_schema = arguments["properties"]["items"]["items"]

    assert arguments["required"] == ["items"]
    assert item_schema["properties"]["type"]["enum"] == ["电影", "电视剧", "音乐"]
    assert item_schema["properties"]["year"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
    ]
    assert set(item_schema["properties"]) == {
        "title",
        "year",
        "type",
        "category",
        "target_path",
    }
    assert item_schema["additionalProperties"] is False


def test_direct_manager_preserves_service_operation_mcp_schema() -> None:
    """工具管理器不得把服务操作的 oneOf 和嵌套 schema 压平成普通对象。"""
    manager = MoviePilotToolsManager(session_id="session", user_id="api_user")
    manager.tools = [DownloaderOperationTool(session_id="session", user_id="api_user")]

    definition = manager.list_tools()[0]

    assert definition.name == "downloader_operation"
    assert definition.input_schema["oneOf"]
    assert _branch(definition.input_schema, "tasks.files")["properties"]["arguments"][
        "required"
    ] == ["task_id"]


def test_factory_only_adds_service_operation_tools_for_external_manager() -> None:
    """内置 Agent 保持 Skill 按需加载，外部管理入口才增加两个常驻工具。"""
    with (
        patch.object(MoviePilotToolFactory, "BUILTIN_TOOL_CLASSES", ()),
        patch("app.agent.tools.factory._get_plugin_agent_tools", return_value=[]),
    ):
        internal = MoviePilotToolFactory.create_tools(
            session_id="session",
            user_id="user",
        )
        external = MoviePilotToolFactory.create_tools(
            session_id="session",
            user_id="api_user",
            include_external_service_tools=True,
        )

    internal_names = {tool.name for tool in internal}
    external_names = {tool.name for tool in external}
    assert "downloader_operation" not in internal_names
    assert "mediaserver_operation" not in internal_names
    assert {"downloader_operation", "mediaserver_operation"}.issubset(external_names)


def test_service_operation_tool_calls_fixed_script_once() -> None:
    """一次 MCP tools/call 应只执行一次固定脚本并返回其 JSON envelope。"""
    tool = DownloaderOperationTool(session_id="session", user_id="api_user")
    with patch(
        "app.agent.tools.impl.service_operation._run_service_script",
        return_value={"success": True, "action": "tasks.list", "data": {"items": []}},
    ) as runner:
        result = asyncio.run(
            tool.run(
                client="main",
                action="tasks.list",
                arguments={"limit": 20},
            )
        )

    assert json.loads(result)["success"] is True
    runner.assert_called_once_with(
        relative_script="skills/downloader-operation/scripts/mp-downloader.py",
        selector_flag="--client",
        selector_value="main",
        action="tasks.list",
        arguments={"limit": 20},
    )
