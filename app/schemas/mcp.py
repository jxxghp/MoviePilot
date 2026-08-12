from typing import Dict, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, Field, RootModel, TypeAdapter

from app.schemas.common import JsonData


class ToolCallRequest(BaseModel):
    """工具调用请求模型"""
    tool_name: str = Field(..., description="工具名称")
    arguments: Dict[str, JsonData] = Field(default_factory=dict, description="工具参数")


class McpJsonSchema(RootModel[dict[str, JsonData]]):
    """MCP 工具的 JSON Schema。"""


class McpToolInfo(BaseModel):
    """MCP REST 工具摘要。"""

    name: str = Field(description="工具名称")
    description: str = Field(default="", description="工具说明")
    inputSchema: McpJsonSchema = Field(description="工具参数 JSON Schema")


class ToolCallData(BaseModel):
    """MCP REST 工具调用成功后的业务数据。"""

    result: str = Field(description="工具执行结果")


class McpJsonRpcClientInfo(BaseModel):
    """MCP JSON-RPC 客户端信息。"""

    name: str
    version: Optional[str] = None


class McpJsonRpcInitializeParams(BaseModel):
    """MCP initialize 请求参数。"""

    protocolVersion: str
    capabilities: dict[str, JsonData] = Field(default_factory=dict)
    clientInfo: Optional[McpJsonRpcClientInfo] = None


class McpJsonRpcToolCallParams(BaseModel):
    """MCP tools/call 请求参数。"""

    name: str
    arguments: dict[str, JsonData] = Field(default_factory=dict)


class McpJsonRpcInitializeRequest(BaseModel):
    """MCP initialize JSON-RPC 请求。"""

    jsonrpc: Literal["2.0"]
    id: str | int
    method: Literal["initialize"]
    params: McpJsonRpcInitializeParams


class McpJsonRpcInitializedNotification(BaseModel):
    """MCP initialized JSON-RPC 通知。"""

    jsonrpc: Literal["2.0"]
    method: Literal["notifications/initialized"]
    params: dict[str, JsonData] = Field(default_factory=dict)


class McpJsonRpcToolsListRequest(BaseModel):
    """MCP tools/list JSON-RPC 请求。"""

    jsonrpc: Literal["2.0"]
    id: str | int
    method: Literal["tools/list"]
    params: dict[str, JsonData] = Field(default_factory=dict)


class McpJsonRpcToolsCallRequest(BaseModel):
    """MCP tools/call JSON-RPC 请求。"""

    jsonrpc: Literal["2.0"]
    id: str | int
    method: Literal["tools/call"]
    params: McpJsonRpcToolCallParams


class McpJsonRpcPingRequest(BaseModel):
    """MCP ping JSON-RPC 请求。"""

    jsonrpc: Literal["2.0"]
    id: str | int
    method: Literal["ping"]
    params: dict[str, JsonData] = Field(default_factory=dict)


McpJsonRpcRequest: TypeAlias = Union[
    McpJsonRpcInitializeRequest,
    McpJsonRpcInitializedNotification,
    McpJsonRpcToolsListRequest,
    McpJsonRpcToolsCallRequest,
    McpJsonRpcPingRequest,
]


class McpJsonRpcServerInfo(BaseModel):
    """MCP 服务器信息。"""

    name: str
    version: str
    description: Optional[str] = None


class McpJsonRpcToolsCapability(BaseModel):
    """MCP 工具能力声明。"""

    listChanged: bool = False


class McpJsonRpcCapabilities(BaseModel):
    """MCP 服务器能力声明。"""

    tools: McpJsonRpcToolsCapability
    logging: dict[str, JsonData] = Field(default_factory=dict)


class McpJsonRpcInitializeResult(BaseModel):
    """MCP initialize 响应结果。"""

    protocolVersion: str
    capabilities: McpJsonRpcCapabilities
    serverInfo: McpJsonRpcServerInfo
    instructions: str


class McpJsonRpcToolsListResult(BaseModel):
    """MCP tools/list 响应结果。"""

    tools: list[McpToolInfo] = Field(default_factory=list)


class McpJsonRpcTextContent(BaseModel):
    """MCP 工具调用文本内容块。"""

    type: Literal["text"] = "text"
    text: str


class McpJsonRpcToolCallResult(BaseModel):
    """MCP tools/call 响应结果。"""

    content: list[McpJsonRpcTextContent] = Field(default_factory=list)
    isError: bool = False


class McpJsonRpcEmptyResult(BaseModel):
    """MCP ping 的空结果。"""


class McpJsonRpcSuccess(BaseModel):
    """MCP JSON-RPC 成功响应。"""

    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[str | int] = None
    result: Union[
        McpJsonRpcInitializeResult,
        McpJsonRpcToolsListResult,
        McpJsonRpcToolCallResult,
        McpJsonRpcEmptyResult,
    ]


class McpJsonRpcErrorDetail(BaseModel):
    """MCP JSON-RPC 错误详情。"""

    code: int
    message: str
    data: Optional[JsonData] = None


class McpJsonRpcError(BaseModel):
    """MCP JSON-RPC 错误响应。"""

    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[str | int] = None
    error: McpJsonRpcErrorDetail


McpJsonRpcResponse: TypeAlias = Union[McpJsonRpcSuccess, McpJsonRpcError]

# 保持协议端点自行解析并返回 JSON-RPC 错误，同时从同一组 Pydantic 模型生成请求文档。
MCP_JSONRPC_REQUEST_SCHEMA = TypeAdapter(McpJsonRpcRequest).json_schema()
