"""LLM 配置、目录和测试 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class LLMAuthStatus(BaseModel):
    """LLM 提供商授权状态摘要。"""

    connected: bool = False
    type: Optional[str] = None
    label: Optional[str] = None
    expires_at: Optional[int | float | str] = None
    updated_at: Optional[int | float | str] = None


class LLMServerToolCapability(BaseModel):
    """模型支持的服务端工具能力。"""

    id: str
    required_api_protocol: Optional[str] = None
    client_adapter: Optional[str] = None


class LLMModelInfo(BaseModel):
    """标准化 LLM 模型目录项。"""

    id: str
    name: str
    family: Optional[str] = None
    context_tokens: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    context_tokens_k: Optional[int] = None
    supports_reasoning: bool = False
    supports_tools: bool = False
    supports_image_input: bool = False
    supports_audio_input: bool = False
    transport: Optional[str] = None
    source: Optional[str] = None
    release_date: Optional[str] = None
    status: Optional[str] = None
    server_tools: list[LLMServerToolCapability] = Field(default_factory=list)


class LLMModelCatalogData(BaseModel):
    """指定提供商的模型目录。"""

    provider: str
    models: list[LLMModelInfo] = Field(default_factory=list)
    auth_status: LLMAuthStatus


class LLMProviderAuthMethod(BaseModel):
    """LLM 提供商可用的交互授权方式。"""

    id: str
    type: str
    label: str
    description: Optional[str] = None


class LLMProviderBaseUrlPreset(BaseModel):
    """LLM 提供商预设基础地址。"""

    id: str
    label: str
    value: str
    runtime: Optional[str] = None
    model_list_strategy: Optional[str] = None


class LLMProviderInfo(BaseModel):
    """前端可配置的 LLM 提供商定义。"""

    id: str
    name: str
    runtime: str
    default_base_url: str = ""
    base_url_presets: list[LLMProviderBaseUrlPreset] = Field(default_factory=list)
    base_url_editable: bool = True
    requires_base_url: bool = False
    supports_api_key: bool = True
    api_key_label: Optional[str] = None
    api_key_hint: Optional[str] = None
    supports_model_refresh: bool = True
    oauth_methods: list[LLMProviderAuthMethod] = Field(default_factory=list)
    description: Optional[str] = None
    auth_status: LLMAuthStatus


class LLMProviderAuthSession(BaseModel):
    """LLM 提供商交互授权会话。"""

    session_id: str
    provider_id: Optional[str] = None
    flow_type: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    authorize_url: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    instructions: Optional[str] = None
    interval_seconds: Optional[int] = None
    expires_at: Optional[int | float] = None


class LLMTestResult(BaseModel):
    """LLM 连通性测试结果。"""

    provider: str
    model: str
    duration_ms: Optional[int] = None
    reply_preview: Optional[str] = None
