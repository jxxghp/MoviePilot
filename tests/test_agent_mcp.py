import sys
import textwrap

import pytest

from app.agent.mcp import AgentMcpManager, AgentMcpToolSpec
from app.agent.tools.impl.mcp import McpExternalTool
from app.schemas.agent import AgentMcpServerConfig


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
