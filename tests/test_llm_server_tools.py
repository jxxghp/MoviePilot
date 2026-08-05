"""LLM 服务端工具能力解析测试。"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.llm import LLMHelper
from app.agent.llm.provider import LLMProviderManager
from app.agent.llm.server_tools import (
    ServerToolRegistry,
    ServerToolUnavailableError,
)


def test_deepseek_v4_flash_exposes_builtin_web_search() -> None:
    """DeepSeek V4 Flash 应声明 Responses 服务端联网搜索能力。"""
    capabilities = ServerToolRegistry.list_capabilities(
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert capabilities == [
        {
            "id": "web_search",
            "required_api_protocol": "responses",
            "client_adapter": "openai_responses",
        }
    ]


@pytest.mark.parametrize(
    (
        "provider",
        "model",
        "base_url",
        "expected_tool",
        "required_api_protocol",
        "client_adapter",
    ),
    [
        (
            "chatgpt",
            "gpt-5.6-sol",
            "https://api.openai.com/v1",
            {"type": "web_search"},
            "responses",
            "openai_responses",
        ),
        (
            "openai",
            "gpt-4.1-mini",
            "https://api.openai.com/v1",
            {"type": "web_search"},
            "responses",
            "openai_responses",
        ),
        (
            "anthropic",
            "claude-opus-5",
            "https://api.anthropic.com/v1",
            {"type": "web_search_20250305", "name": "web_search"},
            "native",
            "anthropic_native",
        ),
        (
            "google",
            "models/gemini-3.6-flash-preview",
            None,
            {"google_search": {}},
            "native",
            "google_native",
        ),
        (
            "xai",
            "grok-4.5",
            "https://api.x.ai/v1",
            {"type": "web_search"},
            "responses",
            "openai_responses",
        ),
    ],
)
def test_official_provider_models_expose_builtin_web_search(
    provider: str,
    model: str,
    base_url: str | None,
    expected_tool: dict,
    required_api_protocol: str,
    client_adapter: str,
) -> None:
    """官方文档声明支持的模型应返回各自原生服务端搜索工具。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider=provider,
        model=model,
        mode="builtin",
        api_protocol="auto",
        base_url=base_url,
    )

    assert resolution.server_tools == (expected_tool,)
    assert resolution.required_api_protocol == required_api_protocol
    assert resolution.client_adapter == client_adapter
    assert resolution.use_local_web_search is False
    assert resolution.available is True


def test_builtin_web_search_selects_responses_adapter() -> None:
    """服务端搜索应切换到通用 Responses 适配器并关闭本地搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="deepseek",
        model="deepseek-v4-flash",
        mode="builtin",
        api_protocol="auto",
    )

    assert resolution.server_tools == ({"type": "web_search"},)
    assert resolution.client_adapter == "openai_responses"
    assert resolution.required_api_protocol == "responses"
    assert resolution.use_local_web_search is False


def test_auto_web_search_falls_back_to_local_for_unsupported_model() -> None:
    """自动模式在模型不支持服务端搜索时应保留本地搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="deepseek",
        model="deepseek-chat",
        mode="auto",
        api_protocol="auto",
    )

    assert resolution.server_tools == ()
    assert resolution.use_local_web_search is True
    assert resolution.reason == "builtin_web_search_unavailable"


def test_auto_web_search_respects_chat_completions_selection() -> None:
    """显式 Chat Completions 下自动模式应回退本地搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="deepseek",
        model="deepseek-v4-flash",
        mode="auto",
        api_protocol="chat_completions",
    )

    assert resolution.server_tools == ()
    assert resolution.use_local_web_search is True
    assert resolution.available is True


def test_native_web_search_ignores_openai_chat_completions_selection() -> None:
    """原生 Gemini 服务端搜索不应被 OpenAI 协议选项误伤回退。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="google",
        model="gemini-3.6-flash-preview",
        mode="auto",
        api_protocol="chat_completions",
    )

    assert resolution.server_tools == ({"google_search": {}},)
    assert resolution.use_local_web_search is False
    assert resolution.available is True


def test_builtin_web_search_does_not_silently_fall_back() -> None:
    """强制服务端模式在模型不支持时不应静默启用本地搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="deepseek",
        model="deepseek-v4-pro",
        mode="builtin",
        api_protocol="auto",
    )

    assert resolution.server_tools == ()
    assert resolution.use_local_web_search is False
    assert resolution.available is False


def test_deepseek_builtin_web_search_is_limited_to_official_endpoint() -> None:
    """自定义 DeepSeek 兼容端点不应被误判为官方托管搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider="deepseek",
        model="deepseek-v4-flash",
        mode="auto",
        api_protocol="auto",
        base_url="https://deepseek-proxy.example.com/v1",
    )

    assert resolution.server_tools == ()
    assert resolution.use_local_web_search is True


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("openai", "gpt-5.6-sol", "https://openai-proxy.example.com/v1"),
        ("anthropic", "claude-opus-5", "https://anthropic-proxy.example.com/v1"),
        ("xai", "grok-4.5", "https://xai-proxy.example.com/v1"),
    ],
)
def test_provider_web_search_is_limited_to_official_endpoints(
    provider: str,
    model: str,
    base_url: str,
) -> None:
    """第三方兼容端点不应被误判为厂商官方托管搜索。"""
    resolution = ServerToolRegistry.resolve_web_search(
        provider=provider,
        model=model,
        mode="auto",
        api_protocol="auto",
        base_url=base_url,
    )

    assert resolution.server_tools == ()
    assert resolution.use_local_web_search is True


@pytest.mark.parametrize(
    ("provider", "model", "runtime_name", "base_url", "expected_tool"),
    [
        (
            "chatgpt",
            "gpt-5.6-sol",
            "openai_compatible",
            "https://api.openai.com/v1",
            {"type": "web_search"},
        ),
        (
            "anthropic",
            "claude-opus-5",
            "anthropic_compatible",
            "https://api.anthropic.com/v1",
            {"type": "web_search_20250305", "name": "web_search"},
        ),
        (
            "google",
            "gemini-3.6-flash-preview",
            "google",
            None,
            {"google_search": {}},
        ),
        (
            "xai",
            "grok-4.5",
            "openai_compatible",
            "https://api.x.ai/v1",
            {"type": "web_search"},
        ),
    ],
)
def test_llm_helper_binds_each_native_server_search_tool_offline(
    provider: str,
    model: str,
    runtime_name: str,
    base_url: str | None,
    expected_tool: dict,
) -> None:
    """LLM Helper 应能离线构造并绑定各厂商的原生搜索工具。"""
    runtime = {
        "provider_id": provider,
        "runtime": runtime_name,
        "model_id": model,
        "api_key": "test-key",
        "base_url": base_url,
        "default_headers": None,
        "use_responses_api": None,
        "model_record": None,
        "model_metadata": None,
    }

    with patch.object(
        LLMProviderManager,
        "resolve_runtime",
        new=AsyncMock(return_value=runtime),
    ):
        llm = asyncio.run(
            LLMHelper.get_llm(
                provider=provider,
                model=model,
                api_key="test-key",
                base_url=base_url,
                web_search_mode="builtin",
            )
        )

    tools = LLMHelper.get_server_tools(llm)
    assert tools == [expected_tool]
    assert llm.bind_tools(tools) is not None


def test_unavailable_server_tool_error_guides_user_to_safe_modes() -> None:
    """服务端搜索不可用时应明确告知用户可选的回退模式。"""
    error = ServerToolUnavailableError(
        provider="deepseek",
        model="deepseek-chat",
        tool_id="web_search",
    )

    assert error.provider == "deepseek"
    assert error.model == "deepseek-chat"
    assert error.tool_id == "web_search"
    assert "不支持服务端联网搜索" in str(error)
    assert "自动" in str(error)
    assert "MoviePilot 本地搜索" in str(error)
