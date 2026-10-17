"""
LLM 服务端工具能力注册与解析。
"""

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Optional

WEB_SEARCH_MODES = frozenset({"local", "builtin", "auto", "disabled"})


class ServerToolUnavailableError(ValueError):
    """
    表示用户强制选择了当前模型不可用的服务端工具。
    """

    def __init__(self, *, provider: str, model: str, tool_id: str) -> None:
        """
        初始化服务端工具不可用异常。
        """
        self.provider = provider
        self.model = model
        self.tool_id = tool_id
        super().__init__(
            f"当前模型 {provider}/{model} 或接口地址不支持服务端联网搜索，"
            "请改用“自动”或“MoviePilot 本地搜索”"
        )


@dataclass(frozen=True)
class ServerToolCapability:
    """
    描述一个模型可用的服务端工具能力。

    :param tool_id: 模型服务端工具的稳定能力标识
    :param provider_ids: 支持该能力的规范化提供商标识集合
    :param model_patterns: 允许使用该能力的模型名称通配模式
    :param required_api_protocol: 调用该能力要求的模型 API 协议
    :param client_adapter: 构造服务端工具请求使用的客户端适配类型
    :param tool_definition: 提交给模型服务的原生工具声明
    :param base_url_patterns: 允许使用该能力的基础地址子串；为空时不限制地址
    :param match_without_base_url: 未配置基础地址时是否允许匹配，默认 True
    """

    tool_id: str
    provider_ids: tuple[str, ...]
    model_patterns: tuple[str, ...]
    required_api_protocol: str
    client_adapter: str
    tool_definition: dict[str, Any]
    base_url_patterns: tuple[str, ...] = ()
    match_without_base_url: bool = True

    def matches(self, provider: str, model: str, base_url: Optional[str] = None) -> bool:
        """
        判断给定 provider/model 是否匹配当前能力。
        """
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip().lower().removeprefix("models/")
        normalized_base_url = str(base_url or "").strip().lower()
        return (
            normalized_provider in self.provider_ids
            and any(fnmatch(normalized_model, pattern) for pattern in self.model_patterns)
            and (
                (not normalized_base_url and self.match_without_base_url)
                or not self.base_url_patterns
                or any(
                    pattern in normalized_base_url
                    for pattern in self.base_url_patterns
                )
            )
        )

    def serialize(self) -> dict[str, Any]:
        """
        返回供 API 与前端使用的能力元数据。
        """
        return {
            "id": self.tool_id,
            "required_api_protocol": self.required_api_protocol,
            "client_adapter": self.client_adapter,
        }


@dataclass(frozen=True)
class ServerToolResolution:
    """
    记录本次联网搜索模式解析后的执行策略。

    :param mode: 规范化后的联网搜索模式：local、builtin、auto 或 disabled
    :param use_local_web_search: 是否向本轮模型提供 MoviePilot 本地联网搜索工具
    :param server_tools: 需要提交给模型服务的原生工具声明；不使用时为空元组
    :param client_adapter: 原生工具使用的客户端适配类型；不使用时为 None
    :param required_api_protocol: 原生工具要求的 API 协议；没有要求时为 None
    :param available: 当前模型和接口地址是否具备所选服务端工具能力
    :param reason: 能力不可用或选择该策略的原因；无需说明时为 None
    """

    mode: str
    use_local_web_search: bool
    server_tools: tuple[dict[str, Any], ...] = ()
    client_adapter: Optional[str] = None
    required_api_protocol: Optional[str] = None
    available: bool = False
    reason: Optional[str] = None


class ServerToolRegistry:
    """
    集中注册模型服务端工具，并解析通用执行策略。
    """

    _CAPABILITIES = (
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("chatgpt",),
            model_patterns=("gpt-5*", "gpt-4.1*", "o4-mini*"),
            base_url_patterns=("api.openai.com",),
            required_api_protocol="responses",
            client_adapter="openai_responses",
            tool_definition={"type": "web_search"},
        ),
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("openai",),
            model_patterns=("gpt-5*", "gpt-4.1*", "o4-mini*"),
            base_url_patterns=("api.openai.com",),
            required_api_protocol="responses",
            client_adapter="openai_responses",
            tool_definition={"type": "web_search"},
            match_without_base_url=False,
        ),
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("anthropic",),
            model_patterns=(
                "claude-opus-4*",
                "claude-sonnet-4*",
                "claude-haiku-4*",
                "claude-opus-5*",
                "claude-sonnet-5*",
                "claude-haiku-5*",
                "claude-fable-5*",
                "claude-mythos-5*",
            ),
            base_url_patterns=("api.anthropic.com",),
            required_api_protocol="native",
            client_adapter="anthropic_native",
            tool_definition={
                "type": "web_search_20250305",
                "name": "web_search",
            },
        ),
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("google",),
            model_patterns=("gemini-3*", "gemini-2.5*", "gemini-2.0-flash*"),
            required_api_protocol="native",
            client_adapter="google_native",
            tool_definition={"google_search": {}},
        ),
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("xai",),
            model_patterns=("grok-4.5*",),
            base_url_patterns=("api.x.ai",),
            required_api_protocol="responses",
            client_adapter="openai_responses",
            tool_definition={"type": "web_search"},
        ),
        ServerToolCapability(
            tool_id="web_search",
            provider_ids=("deepseek",),
            model_patterns=("deepseek-v4-flash",),
            base_url_patterns=("api.deepseek.com",),
            required_api_protocol="responses",
            client_adapter="openai_responses",
            tool_definition={"type": "web_search"},
        ),
    )

    @classmethod
    def normalize_web_search_mode(cls, mode: Optional[str]) -> str:
        """
        规范化联网搜索模式，未知值回退为本地搜索。
        """
        normalized = str(mode or "local").strip().lower()
        return normalized if normalized in WEB_SEARCH_MODES else "local"

    @classmethod
    def get_capability(
        cls,
        *,
        provider: str,
        model: str,
        base_url: Optional[str] = None,
        tool_id: str,
    ) -> Optional[ServerToolCapability]:
        """
        查找指定模型的服务端工具能力。
        """
        return next(
            (
                capability
                for capability in cls._CAPABILITIES
                if capability.tool_id == tool_id
                and capability.matches(provider, model, base_url)
            ),
            None,
        )

    @classmethod
    def list_capabilities(
            cls,
            *,
            provider: str,
            model: str,
            base_url: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        列出指定模型可用的服务端工具能力。
        """
        return [
            capability.serialize()
            for capability in cls._CAPABILITIES
            if capability.matches(provider, model, base_url)
        ]

    @classmethod
    def resolve_web_search(
        cls,
        *,
        provider: str,
        model: str,
        mode: Optional[str],
        api_protocol: Optional[str],
        base_url: Optional[str] = None,
    ) -> ServerToolResolution:
        """
        解析联网搜索应使用本地工具还是模型服务端工具。
        """
        normalized_mode = cls.normalize_web_search_mode(mode)
        normalized_protocol = str(api_protocol or "auto").strip().lower()
        capability = cls.get_capability(
            provider=provider,
            model=model,
            base_url=base_url,
            tool_id="web_search",
        )

        if normalized_mode == "disabled":
            return ServerToolResolution(
                mode=normalized_mode,
                use_local_web_search=False,
                reason="web_search_disabled",
            )
        if normalized_mode == "local":
            return ServerToolResolution(
                mode=normalized_mode,
                use_local_web_search=True,
                reason="local_web_search_selected",
            )
        if capability is None:
            return ServerToolResolution(
                mode=normalized_mode,
                use_local_web_search=normalized_mode == "auto",
                reason="builtin_web_search_unavailable",
            )
        if (
            normalized_mode == "auto"
            and normalized_protocol == "chat_completions"
            and capability.required_api_protocol == "responses"
        ):
            return ServerToolResolution(
                mode=normalized_mode,
                use_local_web_search=True,
                available=True,
                reason="chat_completions_uses_local_fallback",
            )

        return ServerToolResolution(
            mode=normalized_mode,
            use_local_web_search=False,
            server_tools=(dict(capability.tool_definition),),
            client_adapter=capability.client_adapter,
            required_api_protocol=capability.required_api_protocol,
            available=True,
            reason="builtin_web_search_selected",
        )
