from app.agent.llm.provider import LLMProviderManager


def test_openai_compatible_provider_api_key_hint_only_describes_credentials() -> None:
    """通用 OpenAI 兼容入口的 API Key 提示不应混入 Base URL 配置说明。"""
    provider = LLMProviderManager().get_provider("openai")

    assert provider.requires_base_url is True
    assert provider.api_key_hint == (
        "填写 OpenAI-compatible 服务的 API Key；如服务未启用鉴权，可填写任意占位值。"
    )
    assert "Base URL" not in provider.api_key_hint
