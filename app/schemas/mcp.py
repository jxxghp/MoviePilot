"""
MCP 初始化、工具调用和 JSON-RPC 消息的数据结构。
"""

from typing import Dict, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, Field, RootModel, TypeAdapter

from app.schemas.common import JsonData


class ToolCallRequest(BaseModel):
    """
    工具调用请求模型。
    """
    tool_name: str = Field(..., description="工具名称")
    arguments: Dict[str, JsonData] = Field(default_factory=dict, description="工具参数")


class McpJsonSchema(RootModel[dict[str, JsonData]]):
    """
    MCP 工具的 JSON Schema。
    """


class McpToolInfo(BaseModel):
    """
    MCP REST 工具摘要。
    """

    name: str = Field(description="工具名称")
    description: str = Field(default="", description="工具说明")
    inputSchema: McpJsonSchema = Field(description="工具参数 JSON Schema")


class ToolCallData(BaseModel):
    """
    MCP REST 工具调用成功后的业务数据。
    """

    result: str = Field(description="工具执行结果")


class McpJsonRpcClientInfo(BaseModel):
    """
    MCP JSON-RPC 客户端信息。
    """

    name: str = Field(description="显示名称")
    version: Optional[str] = Field(default=None, description="版本号")


class McpJsonRpcInitializeParams(BaseModel):
    """
    MCP initialize 请求参数。
    """

    protocolVersion: str = Field(description="MCP 协议版本")
    capabilities: dict[str, JsonData] = Field(default_factory=dict, description="协议支持的能力声明")
    clientInfo: Optional[McpJsonRpcClientInfo] = Field(default=None, description="MCP 客户端名称和版本")


class McpJsonRpcToolCallParams(BaseModel):
    """
    MCP tools/call 请求参数。
    """

    name: str = Field(description="要调用的 MCP 工具名称")
    arguments: dict[str, JsonData] = Field(default_factory=dict, description="工具调用参数")


class McpJsonRpcInitializeRequest(BaseModel):
    """
    MCP initialize JSON-RPC 请求。
    """

    jsonrpc: Literal["2.0"] = Field(description="JSON-RPC 协议版本")
    id: str | int = Field(description="JSON-RPC 请求标识，用于关联请求与响应")
    method: Literal["initialize"] = Field(description="JSON-RPC 方法名称")
    params: McpJsonRpcInitializeParams = Field(description="请求参数")


class McpJsonRpcInitializedNotification(BaseModel):
    """
    MCP initialized JSON-RPC 通知。
    """

    jsonrpc: Literal["2.0"] = Field(description="JSON-RPC 协议版本")
    method: Literal["notifications/initialized"] = Field(description="JSON-RPC 方法名称")
    params: dict[str, JsonData] = Field(default_factory=dict, description="请求参数")


class McpJsonRpcToolsListRequest(BaseModel):
    """
    MCP tools/list JSON-RPC 请求。
    """

    jsonrpc: Literal["2.0"] = Field(description="JSON-RPC 协议版本")
    id: str | int = Field(description="JSON-RPC 请求标识，用于关联请求与响应")
    method: Literal["tools/list"] = Field(description="JSON-RPC 方法名称")
    params: dict[str, JsonData] = Field(default_factory=dict, description="请求参数")


class McpJsonRpcToolsCallRequest(BaseModel):
    """
    MCP tools/call JSON-RPC 请求。
    """

    jsonrpc: Literal["2.0"] = Field(description="JSON-RPC 协议版本")
    id: str | int = Field(description="JSON-RPC 请求标识，用于关联请求与响应")
    method: Literal["tools/call"] = Field(description="JSON-RPC 方法名称")
    params: McpJsonRpcToolCallParams = Field(description="请求参数")


class McpJsonRpcPingRequest(BaseModel):
    """
    MCP ping JSON-RPC 请求。
    """

    jsonrpc: Literal["2.0"] = Field(description="JSON-RPC 协议版本")
    id: str | int = Field(description="JSON-RPC 请求标识，用于关联请求与响应")
    method: Literal["ping"] = Field(description="JSON-RPC 方法名称")
    params: dict[str, JsonData] = Field(default_factory=dict, description="请求参数")


McpJsonRpcRequest: TypeAlias = Union[
    McpJsonRpcInitializeRequest,
    McpJsonRpcInitializedNotification,
    McpJsonRpcToolsListRequest,
    McpJsonRpcToolsCallRequest,
    McpJsonRpcPingRequest,
]


class McpJsonRpcServerInfo(BaseModel):
    """
    MCP 服务器信息。
    """

    name: str = Field(description="显示名称")
    version: str = Field(description="版本号")
    description: Optional[str] = Field(default=None, description="描述")


class McpJsonRpcToolsCapability(BaseModel):
    """
    MCP 工具能力声明。
    """

    listChanged: bool = Field(default=False, description="是否支持工具列表变化通知")


class McpJsonRpcCapabilities(BaseModel):
    """
    MCP 服务器能力声明。
    """

    tools: McpJsonRpcToolsCapability = Field(description="服务端工具能力声明")
    logging: dict[str, JsonData] = Field(default_factory=dict, description="MCP 日志能力声明")


class McpJsonRpcInitializeResult(BaseModel):
    """
    MCP initialize 响应结果。
    """

    protocolVersion: str = Field(description="MCP 协议版本")
    capabilities: McpJsonRpcCapabilities = Field(description="协议支持的能力声明")
    serverInfo: McpJsonRpcServerInfo = Field(description="MCP 服务端名称和版本")
    instructions: str = Field(description="服务端提供给客户端的使用说明")


class McpJsonRpcToolsListResult(BaseModel):
    """
    MCP tools/list 响应结果。
    """

    tools: list[McpToolInfo] = Field(default_factory=list, description="工具列表")


class McpJsonRpcTextContent(BaseModel):
    """
    MCP 工具调用文本内容块。
    """

    type: Literal["text"] = Field(default="text", description="类型标识")
    text: str = Field(description="文本内容")


class McpJsonRpcToolCallResult(BaseModel):
    """
    MCP tools/call 响应结果。
    """

    content: list[McpJsonRpcTextContent] = Field(default_factory=list, description="消息内容")
    isError: bool = Field(default=False, description="工具调用结果是否表示错误")


class McpJsonRpcEmptyResult(BaseModel):
    """
    MCP ping 的空结果。
    """


class McpJsonRpcSuccess(BaseModel):
    """
    MCP JSON-RPC 成功响应。
    """

    jsonrpc: Literal["2.0"] = Field(default="2.0", description="JSON-RPC 协议版本")
    id: Optional[str | int] = Field(default=None, description="JSON-RPC 请求标识，用于关联请求与响应")
    result: Union[
        McpJsonRpcInitializeResult,
        McpJsonRpcToolsListResult,
        McpJsonRpcToolCallResult,
        McpJsonRpcEmptyResult,
    ] = Field(description="与请求方法对应的成功结果")


class McpJsonRpcErrorDetail(BaseModel):
    """
    MCP JSON-RPC 错误详情。
    """

    code: int = Field(description="错误代码")
    message: str = Field(description="面向调用方的结果说明")
    data: Optional[JsonData] = Field(default=None, description="可选的错误补充信息")


class McpJsonRpcError(BaseModel):
    """
    MCP JSON-RPC 错误响应。
    """

    jsonrpc: Literal["2.0"] = Field(default="2.0", description="JSON-RPC 协议版本")
    id: Optional[str | int] = Field(default=None, description="JSON-RPC 请求标识，用于关联请求与响应")
    error: McpJsonRpcErrorDetail = Field(description="JSON-RPC 错误代码与说明")


McpJsonRpcResponse: TypeAlias = Union[McpJsonRpcSuccess, McpJsonRpcError]

# 保持协议端点自行解析并返回 JSON-RPC 错误，同时从同一组 Pydantic 模型生成请求文档。
MCP_JSONRPC_REQUEST_SCHEMA = TypeAdapter(McpJsonRpcRequest).json_schema()
