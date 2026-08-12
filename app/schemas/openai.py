from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData


class OpenAIModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "moviepilot"


class OpenAIModelListResponse(BaseModel):
    object: str = "list"
    data: List[OpenAIModelInfo] = Field(default_factory=list)


class OpenAIChatMessage(BaseModel):
    """OpenAI Chat Completions 请求中的一条消息。"""

    role: str
    content: JsonData
    name: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class OpenAIChatCompletionsRequest(BaseModel):
    """OpenAI Chat Completions 兼容请求。"""

    model: Optional[str] = None
    messages: List[OpenAIChatMessage]
    user: Optional[str] = None
    stream: bool = False

    model_config = ConfigDict(extra="allow")


class OpenAIResponsesRequest(BaseModel):
    """OpenAI Responses API 兼容请求。"""

    model: Optional[str] = None
    input: JsonData
    instructions: Optional[str] = None
    user: Optional[str] = None
    stream: bool = False

    model_config = ConfigDict(extra="allow")


class OpenAIChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class OpenAIChatChoice(BaseModel):
    index: int = 0
    message: OpenAIChatChoiceMessage
    finish_reason: str = "stop"


class OpenAIUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatChoice]
    usage: OpenAIUsage


class OpenAIResponsesOutputText(BaseModel):
    """Responses API 输出中的文本内容块。"""

    type: str = "output_text"
    text: str
    annotations: List["OpenAIResponseAnnotation"] = Field(default_factory=list)


class OpenAIResponseAnnotation(BaseModel):
    """Responses API 文本内容关联的引用或文件注解。"""

    type: str
    index: Optional[int] = None
    start_index: Optional[int] = None
    end_index: Optional[int] = None
    url: Optional[str] = None
    title: Optional[str] = None
    file_id: Optional[str] = None
    filename: Optional[str] = None


class OpenAIIncompleteDetails(BaseModel):
    """Responses API 未完整结束时的原因。"""

    reason: str


class OpenAIResponsesOutputMessage(BaseModel):
    id: str
    type: str = "message"
    status: str = "completed"
    role: str = "assistant"
    content: List[OpenAIResponsesOutputText] = Field(default_factory=list)


class OpenAIResponsesResponse(BaseModel):
    """OpenAI Responses API 的非流式成功响应。"""

    id: str
    object: str = "response"
    created_at: int
    status: str = "completed"
    model: str
    output: List[OpenAIResponsesOutputMessage] = Field(default_factory=list)
    error: Optional["OpenAIErrorDetail"] = None
    incomplete_details: Optional[OpenAIIncompleteDetails] = None
    usage: OpenAIUsage


class OpenAIErrorDetail(BaseModel):
    message: str
    type: str = "invalid_request_error"
    param: Optional[str] = None
    code: Optional[str] = None


class OpenAIErrorResponse(BaseModel):
    error: OpenAIErrorDetail


OpenAIChatContentPart = Dict[str, JsonData]


class AnthropicMessage(BaseModel):
    """Anthropic Messages 请求中的一条消息。"""

    role: str
    content: JsonData

    model_config = ConfigDict(extra="allow")


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages 兼容请求。"""

    model: Optional[str] = None
    messages: List[AnthropicMessage]
    system: Optional[JsonData] = None
    max_tokens: Optional[int] = 1024
    stream: bool = False

    model_config = ConfigDict(extra="allow")


class AnthropicTextBlock(BaseModel):
    type: str = "text"
    text: str


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicMessagesResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    content: List[AnthropicTextBlock] = Field(default_factory=list)
    model: str
    stop_reason: str = "end_turn"
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


class AnthropicErrorDetail(BaseModel):
    type: str = "invalid_request_error"
    message: str


class AnthropicErrorResponse(BaseModel):
    type: str = "error"
    error: AnthropicErrorDetail
