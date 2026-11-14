"""
LLM 配置、目录和测试 API 输出模型。
"""

from typing import Optional

from pydantic import BaseModel, Field


class LLMAuthStatus(BaseModel):
    """
    LLM 提供商授权状态摘要。
    """

    connected: bool = Field(default=False, description="是否已建立有效授权或连接")
    type: Optional[str] = Field(default=None, description="当前授权方式的类型")
    label: Optional[str] = Field(default=None, description="当前授权方式的显示名称")
    expires_at: Optional[int | float | str] = Field(
        default=None,
        description="授权失效的 Unix 秒级时间戳，兼容字符串形式；未知或无过期时间时为 None",
    )
    updated_at: Optional[int | float | str] = Field(default=None, description="授权记录最近更新的 Unix 秒级时间戳，兼容字符串形式；未知时为 None")


class LLMServerToolCapability(BaseModel):
    """
    模型支持的服务端工具能力。
    """

    id: str = Field(description="服务端工具能力标识")
    required_api_protocol: Optional[str] = Field(default=None, description="使用工具所需的接口协议")
    client_adapter: Optional[str] = Field(default=None, description="调用服务端工具所需的客户端适配器")


class LLMModelInfo(BaseModel):
    """
    标准化 LLM 模型目录项。
    """

    id: str = Field(description="提供商接受的模型标识")
    name: str = Field(description="显示名称")
    family: Optional[str] = Field(default=None, description="模型所属系列")
    context_tokens: Optional[int] = Field(default=None, description="模型上下文窗口的令牌数")
    input_tokens: Optional[int] = Field(default=None, description="输入令牌数")
    output_tokens: Optional[int] = Field(default=None, description="输出令牌数")
    context_tokens_k: Optional[int] = Field(default=None, description="以千个令牌为单位的上下文窗口大小")
    supports_reasoning: bool = Field(default=False, description="是否支持推理模式")
    supports_tools: bool = Field(default=False, description="是否支持工具调用")
    supports_image_input: bool = Field(default=False, description="是否支持图片输入")
    supports_audio_input: bool = Field(default=False, description="是否支持音频输入")
    transport: Optional[str] = Field(default=None, description="模型调用的传输方式")
    source: Optional[str] = Field(default=None, description="数据来源")
    release_date: Optional[str] = Field(default=None, description="发行日期")
    status: Optional[str] = Field(default=None, description="模型目录元数据中的可用性或发布状态；提供商未声明时为 None")
    server_tools: list[LLMServerToolCapability] = Field(default_factory=list, description="模型可用的服务端工具能力")


class LLMModelCatalogData(BaseModel):
    """
    指定提供商的模型目录。
    """

    provider: str = Field(description="模型提供商标识")
    models: list[LLMModelInfo] = Field(default_factory=list, description="可用模型列表")
    auth_status: LLMAuthStatus = Field(description="提供商的授权状态")


class LLMProviderAuthMethod(BaseModel):
    """
    LLM 提供商可用的交互授权方式。
    """

    id: str = Field(description="提供商授权方式标识")
    type: str = Field(description="授权流程类型")
    label: str = Field(description="显示标签")
    description: Optional[str] = Field(default=None, description="描述")


class LLMProviderBaseUrlPreset(BaseModel):
    """
    LLM 提供商预设基础地址。
    """

    id: str = Field(description="基础地址预设标识")
    label: str = Field(description="显示标签")
    value: str = Field(description="预设服务基础地址")
    runtime: Optional[str] = Field(default=None, description="运行时类型")
    model_list_strategy: Optional[str] = Field(default=None, description="获取模型目录的策略")


class LLMProviderInfo(BaseModel):
    """
    前端可配置的 LLM 提供商定义。
    """

    id: str = Field(description="模型提供商标识")
    name: str = Field(description="显示名称")
    runtime: str = Field(description="运行时类型")
    default_base_url: str = Field(default="", description="提供商默认服务基础地址")
    base_url_presets: list[LLMProviderBaseUrlPreset] = Field(default_factory=list, description="可选的服务基础地址预设")
    base_url_editable: bool = Field(default=True, description="是否允许修改服务基础地址")
    requires_base_url: bool = Field(default=False, description="是否必须提供服务基础地址")
    supports_api_key: bool = Field(default=True, description="是否支持使用接口密钥授权")
    api_key_label: Optional[str] = Field(default=None, description="接口密钥的界面显示名称")
    api_key_hint: Optional[str] = Field(default=None, description="接口密钥输入提示")
    supports_model_refresh: bool = Field(default=True, description="是否支持刷新模型目录")
    oauth_methods: list[LLMProviderAuthMethod] = Field(default_factory=list, description="可用的交互授权方式")
    description: Optional[str] = Field(default=None, description="描述")
    auth_status: LLMAuthStatus = Field(description="提供商的授权状态")


class LLMProviderAuthSession(BaseModel):
    """
    LLM 提供商交互授权会话。
    """

    session_id: str = Field(description="会话标识")
    provider_id: Optional[str] = Field(default=None, description="模型提供商标识")
    flow_type: Optional[str] = Field(default=None, description="交互授权流程类型")
    status: Optional[str] = Field(default=None, description="交互授权会话的当前阶段；未提供时为 None，具体状态由授权流程决定")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")
    authorize_url: Optional[str] = Field(default=None, description="用户完成授权的页面地址")
    verification_url: Optional[str] = Field(default=None, description="用户输入授权码的页面地址")
    user_code: Optional[str] = Field(default=None, description="需要在提供商授权页面输入的用户码")
    instructions: Optional[str] = Field(default=None, description="面向用户的授权操作说明")
    interval_seconds: Optional[int] = Field(default=None, description="建议的轮询间隔秒数")
    expires_at: Optional[int | float] = Field(default=None, description="本次交互授权会话的过期 Unix 时间戳，单位为秒；未提供时为 None")


class LLMTestResult(BaseModel):
    """
    LLM 连通性测试结果。
    """

    provider: str = Field(description="模型提供商标识")
    model: str = Field(description="模型名称")
    duration_ms: Optional[int] = Field(default=None, description="测试请求耗时，单位为毫秒")
    reply_preview: Optional[str] = Field(default=None, description="模型测试回复的预览文本")
