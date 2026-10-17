"""
OpenAI 与 Anthropic 兼容接口的请求、响应及错误结构。
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData


class OpenAIModelInfo(BaseModel):
    """
    OpenAI 兼容接口中的模型目录条目。
    """

    id: str = Field(description="模型标识，供生成请求选择模型")
    object: str = Field(default="model", description="响应对象类型标识")
    created: int = Field(description="创建时间的 Unix 时间戳")
    owned_by: str = Field(default="moviepilot", description="模型所属组织")


class OpenAIModelListResponse(BaseModel):
    """
    OpenAI 兼容接口返回的模型列表。
    """

    object: str = Field(default="list", description="响应对象类型标识")
    data: List[OpenAIModelInfo] = Field(default_factory=list, description="可用模型目录列表")


class OpenAIChatMessage(BaseModel):
    """
    OpenAI Chat Completions 请求中的一条消息。
    """

    role: str = Field(description="消息角色")
    content: JsonData = Field(description="消息内容")
    name: Optional[str] = Field(default=None, description="消息发送者名称，未指定时省略")

    model_config = ConfigDict(extra="allow")


class OpenAIChatCompletionsRequest(BaseModel):
    """
    OpenAI Chat Completions 兼容请求。
    """

    model: Optional[str] = Field(default=None, description="模型名称")
    messages: List[OpenAIChatMessage] = Field(description="按对话顺序排列的输入消息")
    user: Optional[str] = Field(default=None, description="发起用户")
    stream: bool = Field(default=False, description="是否以流式方式返回生成内容")

    model_config = ConfigDict(extra="allow")


class OpenAIResponsesRequest(BaseModel):
    """
    OpenAI Responses API 兼容请求。
    """

    model: Optional[str] = Field(default=None, description="模型名称")
    input: JsonData = Field(description="模型输入内容")
    instructions: Optional[str] = Field(default=None, description="提供给模型的系统或开发者指令")
    user: Optional[str] = Field(default=None, description="发起用户")
    stream: bool = Field(default=False, description="是否以流式方式返回生成内容")

    model_config = ConfigDict(extra="allow")


class OpenAIChatChoiceMessage(BaseModel):
    """
    模型生成的聊天回复消息。
    """

    role: str = Field(default="assistant", description="消息角色")
    content: str = Field(description="消息内容")


class OpenAIChatChoice(BaseModel):
    """
    聊天生成结果中的一个候选回复。
    """

    index: int = Field(default=0, description="候选回复的序号，从 0 开始")
    message: OpenAIChatChoiceMessage = Field(description="该候选生成的回复消息")
    finish_reason: str = Field(default="stop", description="模型停止生成的原因")


class OpenAIUsage(BaseModel):
    """
    模型调用的输入、输出与总令牌用量。
    """

    prompt_tokens: int = Field(default=0, description="输入提示消耗的令牌数")
    completion_tokens: int = Field(default=0, description="模型输出消耗的令牌数")
    total_tokens: int = Field(default=0, description="输入和输出的令牌总数")


class OpenAIChatCompletionResponse(BaseModel):
    """
    OpenAI 聊天生成接口的完整响应。
    """

    id: str = Field(description="本次聊天生成请求的标识")
    object: str = Field(default="chat.completion", description="响应对象类型标识")
    created: int = Field(description="创建时间的 Unix 时间戳")
    model: str = Field(description="模型名称")
    choices: List[OpenAIChatChoice] = Field(description="模型生成的候选回复")
    usage: OpenAIUsage = Field(description="生成过程的令牌用量")


class OpenAIResponsesOutputText(BaseModel):
    """
    Responses API 输出中的文本内容块。
    """

    type: str = Field(default="output_text", description="类型标识")
    text: str = Field(description="文本内容")
    annotations: List["OpenAIResponseAnnotation"] = Field(default_factory=list, description="输出文本附带的来源注解")


class OpenAIResponseAnnotation(BaseModel):
    """
    Responses API 文本内容关联的引用或文件注解。
    """

    type: str = Field(description="类型标识")
    index: Optional[int] = Field(default=None, description="结果或注解在列表中的序号")
    start_index: Optional[int] = Field(default=None, description="注解对应文本的开始位置")
    end_index: Optional[int] = Field(default=None, description="注解对应文本的结束位置")
    url: Optional[str] = Field(default=None, description="访问地址")
    title: Optional[str] = Field(default=None, description="标题")
    file_id: Optional[str] = Field(default=None, description="注解引用的文件标识")
    filename: Optional[str] = Field(default=None, description="文件名")


class OpenAIIncompleteDetails(BaseModel):
    """
    Responses API 未完整结束时的原因。
    """

    reason: str = Field(description="生成未完整结束的原因")


class OpenAIResponsesOutputMessage(BaseModel):
    """
    Responses 接口返回的一条输出消息。
    """

    id: str = Field(description="输出消息标识")
    type: str = Field(default="message", description="类型标识")
    status: str = Field(default="completed", description="当前处理状态")
    role: str = Field(default="assistant", description="消息角色")
    content: List[OpenAIResponsesOutputText] = Field(default_factory=list, description="消息内容")


class OpenAIResponsesResponse(BaseModel):
    """
    OpenAI Responses API 的非流式成功响应。
    """

    id: str = Field(description="本次生成响应的标识")
    object: str = Field(default="response", description="响应对象类型标识")
    created_at: int = Field(description="响应创建时间的 Unix 秒级时间戳")
    status: str = Field(default="completed", description="当前处理状态")
    model: str = Field(description="模型名称")
    output: List[OpenAIResponsesOutputMessage] = Field(default_factory=list, description="模型生成的输出条目")
    error: Optional["OpenAIErrorDetail"] = Field(default=None, description="错误摘要")
    incomplete_details: Optional[OpenAIIncompleteDetails] = Field(default=None, description="生成未完成时的原因详情")
    usage: OpenAIUsage = Field(description="生成过程的令牌用量")


class OpenAIErrorDetail(BaseModel):
    """
    OpenAI 兼容接口返回的错误详情。
    """

    message: str = Field(description="面向调用方的结果说明")
    type: str = Field(default="invalid_request_error", description="类型标识")
    param: Optional[str] = Field(default=None, description="导致错误的请求参数名称")
    code: Optional[str] = Field(default=None, description="错误代码")


class OpenAIErrorResponse(BaseModel):
    """
    OpenAI 兼容接口的错误响应。
    """

    error: OpenAIErrorDetail = Field(description="错误摘要")


OpenAIChatContentPart = Dict[str, JsonData]


class AnthropicMessage(BaseModel):
    """
    Anthropic Messages 请求中的一条消息。
    """

    role: str = Field(description="消息角色")
    content: JsonData = Field(description="消息内容")

    model_config = ConfigDict(extra="allow")


class AnthropicMessagesRequest(BaseModel):
    """
    Anthropic Messages 兼容请求。
    """

    model: Optional[str] = Field(default=None, description="模型名称")
    messages: List[AnthropicMessage] = Field(description="按对话顺序排列的输入消息")
    system: Optional[JsonData] = Field(default=None, description="系统提示内容")
    max_tokens: Optional[int] = Field(default=1024, description="本次生成允许的最大输出令牌数")
    stream: bool = Field(default=False, description="是否以流式方式返回生成内容")

    model_config = ConfigDict(extra="allow")


class AnthropicTextBlock(BaseModel):
    """
    Anthropic 消息中的文本内容块。
    """

    type: str = Field(default="text", description="类型标识")
    text: str = Field(description="文本内容")


class AnthropicUsage(BaseModel):
    """
    Anthropic 调用的输入与输出令牌用量。
    """

    input_tokens: int = Field(default=0, description="输入令牌数")
    output_tokens: int = Field(default=0, description="输出令牌数")


class AnthropicMessagesResponse(BaseModel):
    """
    Anthropic 消息生成接口的完整响应。
    """

    id: str = Field(description="生成消息的标识")
    type: str = Field(default="message", description="类型标识")
    role: str = Field(default="assistant", description="消息角色")
    content: List[AnthropicTextBlock] = Field(default_factory=list, description="消息内容")
    model: str = Field(description="模型名称")
    stop_reason: str = Field(default="end_turn", description="模型停止生成的原因")
    stop_sequence: Optional[str] = Field(default=None, description="触发停止生成的匹配序列")
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage, description="生成过程的令牌用量")


class AnthropicErrorDetail(BaseModel):
    """
    Anthropic 兼容接口返回的错误详情。
    """

    type: str = Field(default="invalid_request_error", description="类型标识")
    message: str = Field(description="面向调用方的结果说明")


class AnthropicErrorResponse(BaseModel):
    """
    Anthropic 兼容接口的错误响应。
    """

    type: str = Field(default="error", description="类型标识")
    error: AnthropicErrorDetail = Field(description="错误摘要")
