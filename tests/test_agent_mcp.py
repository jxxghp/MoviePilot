import sys
import textwrap
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.mcp import AgentMcpManager, AgentMcpToolSpec
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.impl.mcp import (
    McpExternalTool,
    create_external_mcp_tools,
    select_legacy_mcp_tools,
)
from app.schemas.agent import AgentMcpServerConfig


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。

    stdio MCP 服务端的启动与工具调用路径直接使用 ``asyncio`` 子进程原语，
    在 trio 后端下没有 running asyncio loop，必然以
    ``RuntimeError: no running event loop`` 失败。
    """
    return "asyncio"


def _write_stdio_mcp_server(tmp_path):
    """写入一个用于测试的最小 stdio MCP 服务。"""
    server_path = tmp_path / "stdio_mcp_server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            TOOLS = [
                {
                    "name": "echo",
                    "description": "Echo input text.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text to echo"}
                        },
                        "required": ["text"],
                    },
                }
            ]

            for line in sys.stdin:
                request = json.loads(line)
                request_id = request.get("id")
                method = request.get("method")
                if request_id is None:
                    continue
                if method == "initialize":
                    result = {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "Fake MCP", "version": "1.0.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    args = request.get("params", {}).get("arguments", {})
                    result = {"content": [{"type": "text", "text": args.get("text", "")}]}
                else:
                    result = {}
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    return server_path


@pytest.mark.anyio
async def test_stdio_mcp_server_lists_tools(tmp_path):
    """stdio MCP 服务器应能被初始化并读取工具列表。"""
    server_path = _write_stdio_mcp_server(tmp_path)
    manager = AgentMcpManager()
    server = AgentMcpServerConfig(
        id="fake",
        name="Fake MCP",
        transport="stdio",
        command=sys.executable,
        args=[str(server_path)],
        timeout=5,
    )

    tools = await manager.list_server_tools(server)

    assert len(tools) == 1
    assert tools[0].name == "echo"
    assert tools[0].agent_tool_name == "mcp_fake_mcp_echo"
    assert tools[0].input_schema["properties"]["text"]["type"] == "string"


@pytest.mark.anyio
async def test_enabled_specs_preserve_cross_server_name_collisions() -> None:
    """跨 MCP 服务器生成相同 Agent 名时必须把全部身份交给目录判断。"""
    manager = AgentMcpManager()
    first = AgentMcpServerConfig(id="one", name="one", transport="stdio", command="one")
    second = AgentMcpServerConfig(id="two", name="two", transport="stdio", command="two")

    def _spec(server):
        return AgentMcpToolSpec(
            server=server,
            name="echo",
            agent_tool_name="shared_echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
        )

    with patch.object(manager, "get_servers", return_value=[first, second]), patch.object(
        manager,
        "list_server_tools",
        new=AsyncMock(side_effect=lambda server: [_spec(server)]),
    ):
        specs = await manager.list_enabled_tool_specs()

    assert [spec.server.id for spec in specs] == ["one", "two"]


@pytest.mark.anyio
async def test_mcp_catalog_uses_server_id_and_legacy_execution_keeps_first() -> None:
    """目录应区分同显示名服务器，普通执行仍保留首个同名工具。"""
    first_server = AgentMcpServerConfig(
        id="one",
        name="Shared Name",
        transport="stdio",
        command="one",
    )
    second_server = AgentMcpServerConfig(
        id="two",
        name="Shared Name",
        transport="stdio",
        command="two",
    )

    def _spec(server):
        return AgentMcpToolSpec(
            server=server,
            name="echo",
            agent_tool_name="shared_echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
        )

    tools = await create_external_mcp_tools(
        session_id="session",
        user_id="user",
        specs=[_spec(first_server), _spec(second_server)],
    )
    catalog = ToolCatalogSnapshot.from_tools(
        tools,
        plugin_revision=0,
        factory_revision="factory-v1",
    )

    assert [entry.source for entry in catalog.collisions["shared_echo"]] == [
        "mcp:one",
        "mcp:two",
    ]
    assert len({entry.identity for entry in catalog.collisions["shared_echo"]}) == 2
    assert select_legacy_mcp_tools(tools) == [tools[0]]


@pytest.mark.anyio
async def test_mcp_catalog_distinguishes_normalized_names_within_server() -> None:
    """同一服务内规范化重名的原始工具仍应具有不同绑定身份。"""
    server = AgentMcpServerConfig(
        id="shared",
        name="Shared",
        transport="stdio",
        command="shared",
    )

    def _spec(name: str) -> AgentMcpToolSpec:
        return AgentMcpToolSpec(
            server=server,
            name=name,
            agent_tool_name="mcp_shared_foo_bar",
            description="same description",
            input_schema={"type": "object", "properties": {}},
        )

    tools = await create_external_mcp_tools(
        session_id="session",
        user_id="user",
        specs=[_spec("foo-bar"), _spec("foo_bar")],
    )
    catalog = ToolCatalogSnapshot.from_tools(
        tools,
        plugin_revision=0,
        factory_revision="factory-v1",
    )
    first_only = ToolCatalogSnapshot.from_tools(
        [tools[0]],
        plugin_revision=0,
        factory_revision="factory-v1",
    )
    second_only = ToolCatalogSnapshot.from_tools(
        [tools[1]],
        plugin_revision=0,
        factory_revision="factory-v1",
    )

    collisions = catalog.collisions["mcp_shared_foo_bar"]
    assert len({entry.identity for entry in collisions}) == 2
    assert len({entry.revision.implementation for entry in collisions}) == 2
    assert first_only.signature != second_only.signature


@pytest.mark.anyio
async def test_stdio_mcp_server_calls_tool(tmp_path):
    """stdio MCP 工具应能通过 tools/call 返回内容。"""
    server_path = _write_stdio_mcp_server(tmp_path)
    manager = AgentMcpManager()
    server = AgentMcpServerConfig(
        id="fake",
        name="Fake MCP",
        transport="stdio",
        command=sys.executable,
        args=[str(server_path)],
        timeout=5,
    )

    result = await manager.call_server_tool(server, "echo", {"text": "hello"})

    assert result == {"content": [{"type": "text", "text": "hello"}]}


def test_normalize_server_generates_runtime_defaults():
    """MCP 配置规范化应补齐默认值并清理空字段。"""
    manager = AgentMcpManager()

    server = manager.normalize_server(
        {
            "id": "demo",
            "name": "Demo",
            "transport": "http",
            "url": " https://example.com/mcp ",
            "headers": {" Authorization ": "Bearer token", "": "ignored"},
            "timeout": "bad",
        }
    )

    assert server.id == "demo"
    assert server.url == "https://example.com/mcp"
    assert server.headers == {"Authorization": "Bearer token"}
    assert server.timeout == 30
    assert server.require_admin is True


def test_mcp_external_tool_uses_discovered_schema():
    """外部 MCP 工具应保留发现到的 JSON Schema。"""
    server = AgentMcpServerConfig(id="fake", name="Fake MCP")
    spec = AgentMcpToolSpec(
        server=server,
        name="echo",
        agent_tool_name="mcp_fake_echo",
        description="Echo input text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    tool = McpExternalTool(spec=spec, session_id="session-1", user_id="10001")

    assert tool.name == "mcp_fake_echo"
    assert tool.args_schema["properties"]["text"]["type"] == "string"
    assert tool.require_admin is True
