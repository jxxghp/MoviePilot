"""AI智能体相关数据模型"""

from datetime import datetime
from typing import Any, List, Literal, Optional, Union

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, ConfigDict, field_serializer


class ConversationMemory(BaseModel):
    """对话记忆模型"""

    session_id: str = Field(description="会话ID")
    user_id: Optional[str] = Field(default=None, description="用户ID")
    messages: List[BaseMessage] = Field(default_factory=list, description="消息列表")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")

    model_config = ConfigDict()

    @field_serializer('updated_at', when_used='json')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class AgentState(BaseModel):
    """AI智能体状态模型"""

    session_id: str = Field(description="会话ID")
    current_task: Optional[str] = Field(default=None, description="当前任务")
    is_thinking: bool = Field(default=False, description="是否正在思考")
    last_activity: datetime = Field(default_factory=datetime.now, description="最后活动时间")

    model_config = ConfigDict()

    @field_serializer('last_activity', when_used='json')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class UserMessage(BaseModel):
    """用户消息模型"""

    session_id: str = Field(description="会话ID")
    content: str = Field(description="消息内容")
    user_id: Optional[str] = Field(default=None, description="用户ID")
    channel: Optional[str] = Field(default=None, description="消息渠道")
    source: Optional[str] = Field(default=None, description="消息来源")


class ToolResult(BaseModel):
    """工具执行结果模型"""

    session_id: str = Field(description="会话ID")
    call_id: str = Field(description="调用ID")
    success: bool = Field(description="是否成功")
    result: Optional[str] = Field(default=None, description="执行结果")
    error: Optional[str] = Field(default=None, description="错误信息")


class AgentMcpServerConfig(BaseModel):
    """Agent 外部 MCP 服务器配置。"""

    id: str = Field(..., description="服务器唯一 ID")
    name: str = Field(..., description="服务器显示名称")
    enabled: bool = Field(default=True, description="是否启用")
    transport: Literal["stdio", "sse", "http", "streamable_http"] = Field(
        default="stdio", description="MCP 传输协议"
    )
    description: Optional[str] = Field(None, description="服务器说明")
    command: Optional[str] = Field(None, description="stdio 启动命令")
    args: list[str] = Field(default_factory=list, description="stdio 启动参数")
    env: dict[str, str] = Field(default_factory=dict, description="stdio 环境变量")
    url: Optional[str] = Field(None, description="HTTP/SSE MCP 入口地址")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 请求头")
    timeout: int = Field(default=30, description="连接和调用超时时间（秒）")
    tool_prefix: Optional[str] = Field(None, description="注入 Agent 的工具名前缀")
    require_admin: bool = Field(default=True, description="是否仅管理员可调用")


class AgentMcpServersSaveRequest(BaseModel):
    """Agent 外部 MCP 服务器保存请求。"""

    servers: list[AgentMcpServerConfig] = Field(
        default_factory=list, description="MCP 服务器配置列表"
    )


class AgentMcpServerTestRequest(BaseModel):
    """Agent 外部 MCP 服务器测试请求。"""

    server: AgentMcpServerConfig = Field(..., description="待测试的 MCP 服务器配置")


class AgentMcpServerToolInfo(BaseModel):
    """Agent 外部 MCP 工具摘要。"""

    name: str = Field(..., description="原始 MCP 工具名称")
    agent_tool_name: str = Field(..., description="注入 Agent 后的工具名称")
    description: str = Field(default="", description="工具说明")
    input_schema: dict[str, Any] = Field(default_factory=dict, description="工具参数 Schema")


class AgentMcpServerTestResult(BaseModel):
    """Agent 外部 MCP 服务器测试结果。"""

    success: bool = Field(..., description="测试是否成功")
    message: str = Field(default="", description="测试消息")
    tools: list[AgentMcpServerToolInfo] = Field(default_factory=list, description="工具列表")
    tool_count: int = Field(default=0, description="工具数量")


class AgentChatAttachment(BaseModel):
    """
    Agent 会话展示附件。
    """

    kind: str = Field(..., description="附件类型")
    url: str = Field(..., description="附件访问地址")
    download_url: Optional[str] = Field(None, description="附件下载地址")
    name: Optional[str] = Field(None, description="附件名称")
    mime_type: Optional[str] = Field(None, description="MIME 类型")
    size: Optional[int] = Field(None, description="附件大小")
    local_path: Optional[str] = Field(None, description="服务端本地路径")


class AgentChatToolCall(BaseModel):
    """
    Agent 会话工具调用展示项。
    """

    id: str = Field(..., description="展示 ID")
    message: str = Field(..., description="工具提示")
    status: str = Field(default="done", description="工具状态")


class AgentChatChoiceButton(BaseModel):
    """
    Agent 会话选择按钮。
    """

    label: str = Field(..., description="按钮文案")
    callback_data: str = Field(..., description="回调数据")
    description: Optional[str] = Field(None, description="选项描述")


class AgentChatChoiceSelection(BaseModel):
    """
    Agent 会话中用户选择的展示快照。
    """

    choice_id: str = Field(..., description="选择卡片 ID")
    title: Optional[str] = Field(None, description="标题")
    prompt: str = Field(default="", description="提示语")
    buttons: list[AgentChatChoiceButton] = Field(default_factory=list, description="按钮列表")
    button_rows: list[list[AgentChatChoiceButton]] = Field(default_factory=list, description="按钮行")
    selected_label: Optional[str] = Field(None, description="已选择文案")
    selected_value: Optional[str] = Field(None, description="已选择值")
    selected_description: Optional[str] = Field(None, description="已选择描述")


class AgentChatChoiceCard(BaseModel):
    """
    Agent 会话选择卡片。
    """

    id: str = Field(..., description="选择卡片 ID")
    title: Optional[str] = Field(None, description="标题")
    prompt: str = Field(default="", description="提示语")
    buttons: list[AgentChatChoiceButton] = Field(default_factory=list, description="按钮列表")
    button_rows: list[list[AgentChatChoiceButton]] = Field(default_factory=list, description="按钮行")
    status: str = Field(default="pending", description="选择状态")
    selected_label: Optional[str] = Field(None, description="已选择文案")
    selected_value: Optional[str] = Field(None, description="已选择值")
    selected_description: Optional[str] = Field(None, description="已选择描述")


class AgentChatMessage(BaseModel):
    """
    Agent 会话展示消息。
    """

    id: str = Field(..., description="展示消息 ID")
    role: str = Field(..., description="消息角色")
    content: str = Field(default="", description="消息文本")
    createdAt: Union[int, float] = Field(..., description="创建时间戳")
    status: str = Field(default="done", description="消息状态")
    tools: list[AgentChatToolCall] = Field(default_factory=list, description="工具提示列表")
    attachments: list[AgentChatAttachment] = Field(default_factory=list, description="附件列表")
    choices: list[AgentChatChoiceCard] = Field(default_factory=list, description="选择卡片列表")
    choice_selection: Optional[AgentChatChoiceSelection] = Field(None, description="用户选择项快照")


class AgentChatSession(BaseModel):
    """
    Agent 会话历史详情。
    """

    id: Optional[int] = Field(None, description="数据库 ID")
    session_id: str = Field(..., description="Agent 内部会话 ID")
    client_session_id: Optional[str] = Field(None, description="客户端原始会话 ID")
    title: Optional[str] = Field(None, description="会话标题")
    preview: Optional[str] = Field(None, description="会话预览")
    channel: Optional[str] = Field(None, description="消息渠道")
    source: Optional[str] = Field(None, description="渠道来源")
    user_id: Optional[str] = Field(None, description="用户 ID")
    username: Optional[str] = Field(None, description="用户名")
    original_chat_id: Optional[str] = Field(None, description="原聊天 ID")
    message_count: int = Field(default=0, description="展示消息数量")
    created_at: Optional[str] = Field(None, description="创建时间")
    updated_at: Optional[str] = Field(None, description="更新时间")
    messages: list[AgentChatMessage] = Field(default_factory=list, description="展示消息列表")


class AgentChatDisplaySaveRequest(BaseModel):
    """
    Agent 会话展示消息保存请求。
    """

    messages: list[AgentChatMessage] = Field(default_factory=list, description="展示消息列表")
    title: Optional[str] = Field(None, description="会话标题")
