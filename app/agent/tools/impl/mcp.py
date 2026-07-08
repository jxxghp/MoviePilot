"""外部 MCP 工具适配器。"""

import json
from typing import Any, Optional

from pydantic import PrivateAttr

from app.agent.mcp import AgentMcpToolSpec, agent_mcp_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag


class McpExternalTool(MoviePilotTool):
    """将外部 MCP 工具包装为 MoviePilot Agent 工具。"""

    name: str = "mcp_external_tool"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Admin,
    ]
    description: str = "Call an external MCP tool configured for MoviePilot Agent."
    args_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    require_admin: bool = True

    _spec: AgentMcpToolSpec = PrivateAttr()

    def __init__(self, spec: AgentMcpToolSpec, session_id: str, user_id: str) -> None:
        super().__init__(
            session_id=session_id,
            user_id=user_id,
            name=spec.agent_tool_name,
            description=spec.description
            or f"Call external MCP tool {spec.name} on {spec.server.name}.",
            args_schema=spec.input_schema,
            require_admin=spec.server.require_admin,
        )
        self._spec = spec

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据 MCP 工具信息生成友好的提示消息。"""
        return f"调用 MCP 工具: {self._spec.server.name}/{self._spec.name}"

    async def run(self, **kwargs) -> str:
        """
        调用外部 MCP 工具。

        :param kwargs: 传递给外部 MCP 工具的参数
        :return: MCP 工具返回内容
        """
        result = await agent_mcp_manager.call_server_tool(
            server=self._spec.server,
            tool_name=self._spec.name,
            arguments=kwargs,
        )
        return self._format_mcp_result(result)

    @staticmethod
    def _format_mcp_result(result: Any) -> str:
        """将 MCP tools/call 返回结构转换为 Agent 可读文本。"""
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and item.get("text") is not None:
                        parts.append(str(item["text"]))
                    elif item:
                        parts.append(json.dumps(item, ensure_ascii=False, default=str))
                if parts:
                    return "\n".join(parts)
            if result.get("isError"):
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


async def create_external_mcp_tools(
    *,
    session_id: str,
    user_id: str,
    channel: Optional[str] = None,
    source: Optional[str] = None,
    username: Optional[str] = None,
    stream_handler=None,
    agent_context: Optional[dict] = None,
) -> list[McpExternalTool]:
    """创建当前已启用的外部 MCP Agent 工具列表。"""
    tools = []
    for spec in await agent_mcp_manager.list_enabled_tool_specs():
        tool = McpExternalTool(spec=spec, session_id=session_id, user_id=user_id)
        tool.set_message_attr(channel=channel, source=source, username=username)
        tool.set_stream_handler(stream_handler=stream_handler)
        tool.set_agent_context(agent_context=agent_context)
        tools.append(tool)
    return tools
