from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OpenAIModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "moviepilot"


class OpenAIModelListResponse(BaseModel):
    object: str = "list"
    data: List[OpenAIModelInfo] = Field(default_factory=list)


class OpenAIChatMessage(BaseModel):
    role: str
    content: Any
    name: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class OpenAIChatCompletionsRequest(BaseModel):
    model: Optional[str] = None
    messages: List[OpenAIChatMessage]
    user: Optional[str] = None
    stream: bool = False

    model_config = ConfigDict(extra="allow")


class OpenAIResponsesRequest(BaseModel):
    model: Optional[str] = None
    input: Any
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
    type: str = "output_text"
    text: str
    annotations: List[Dict[str, Any]] = Field(default_factory=list)


class OpenAIResponsesOutputMessage(BaseModel):
    id: str
    type: str = "message"
    status: str = "completed"
    role: str = "assistant"
    content: List[OpenAIResponsesOutputText] = Field(default_factory=list)


class OpenAIResponsesResponse(BaseModel):
    id: str
    object: str = "response"
    created_at: int
    status: str = "completed"
    model: str
    output: List[OpenAIResponsesOutputMessage] = Field(default_factory=list)
    error: Optional[Any] = None
    incomplete_details: Optional[Any] = None
    usage: OpenAIUsage


class OpenAIErrorDetail(BaseModel):
    message: str
    type: str = "invalid_request_error"
    param: Optional[str] = None
    code: Optional[str] = None


class OpenAIErrorResponse(BaseModel):
    error: OpenAIErrorDetail


OpenAIChatContentPart = Dict[str, Any]


class AnthropicMessage(BaseModel):
    role: str
    content: Any

    model_config = ConfigDict(extra="allow")


class AnthropicMessagesRequest(BaseModel):
    model: Optional[str] = None
    messages: List[AnthropicMessage]
    system: Optional[Any] = None
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
