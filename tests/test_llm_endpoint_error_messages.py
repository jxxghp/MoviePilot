import asyncio
from unittest.mock import AsyncMock, patch

from app.api.endpoints import llm as llm_endpoint


def test_llm_test_maps_internal_model_dump_error_to_base_url_hint():
    """LLM 测试遇到 SDK 内部响应解析错误时应提示检查基础地址。"""
    with patch.object(llm_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
        llm_endpoint.settings, "LLM_PROVIDER", "openai"
    ), patch.object(llm_endpoint.settings, "LLM_MODEL", "gpt-4o-mini"), patch.object(
        llm_endpoint.settings, "LLM_API_KEY", "sk-test"
    ), patch.object(
        llm_endpoint.settings, "LLM_BASE_URL", "https://example.com/not-api"
    ), patch.object(
        llm_endpoint.LLMHelper,
        "test_current_settings",
        AsyncMock(side_effect=RuntimeError("'str' object has no attribute 'model_dump'")),
        create=True,
    ):
        resp = asyncio.run(llm_endpoint.llm_test(_="token"))

    assert not resp.success
    assert "基础地址" in resp.message
    assert "API Base URL" in resp.message
    assert "model_dump" not in resp.message


def test_llm_test_maps_internal_private_attribute_error_to_base_url_hint():
    """LLM 测试遇到 SDK 内部属性错误时应提示检查基础地址。"""
    with patch.object(llm_endpoint.settings, "AI_AGENT_ENABLE", True), patch.object(
        llm_endpoint.settings, "LLM_PROVIDER", "openai"
    ), patch.object(llm_endpoint.settings, "LLM_MODEL", "gpt-4o-mini"), patch.object(
        llm_endpoint.settings, "LLM_API_KEY", "sk-test"
    ), patch.object(
        llm_endpoint.LLMHelper,
        "test_current_settings",
        AsyncMock(
            side_effect=AttributeError(
                "'str' object has no attribute '_set_private_attributes'"
            )
        ),
        create=True,
    ):
        resp = asyncio.run(llm_endpoint.llm_test(_="token"))

    assert not resp.success
    assert "基础地址" in resp.message
    assert "API Base URL" in resp.message
    assert "_set_private_attributes" not in resp.message


def test_llm_models_maps_internal_private_attribute_error_to_base_url_hint():
    """LLM 模型列表遇到 SDK 内部属性错误时应提示检查基础地址。"""
    with patch.object(
        llm_endpoint.LLMHelper,
        "get_models",
        AsyncMock(
            side_effect=AttributeError(
                "'str' object has no attribute '_set_private_attributes'"
            )
        ),
        create=True,
    ):
        resp = asyncio.run(
            llm_endpoint.get_llm_models(
                provider="openai",
                api_key="sk-test",
                base_url="https://example.com",
                _="token",
            )
        )

    assert not resp.success
    assert "基础地址" in resp.message
    assert "API Base URL" in resp.message
    assert "_set_private_attributes" not in resp.message
