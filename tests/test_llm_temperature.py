"""LLM 温度的未指定、显式留空与数值透传合同。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.agent.llm import helper
from app.agent.llm.helper import LLMHelper
from app.agent.llm.provider import LLMProviderManager


@pytest.fixture
def offline_runtime(monkeypatch):
    runtime = SimpleNamespace(resolve_runtime=AsyncMock(return_value={
        "provider_id": "openai",
        "runtime": "openai_compatible",
        "model_id": "gpt-6-luna",
        "api_key": "offline-key",
        "base_url": "https://offline.example/v1",
        "model_metadata": None,
    }))
    monkeypatch.setattr(helper, "_patch_openai_responses_instructions_support", lambda: None)
    monkeypatch.setattr(helper, "_patch_tool_schema_request_support", lambda *_args, **_kwargs: None)
    clients = []

    def reject_request(_request):
        raise AssertionError("温度序列化测试不得发起外部请求")

    def make_client(_proxy, *, async_client=False):
        client_type = httpx.AsyncClient if async_client else httpx.Client
        client = client_type(transport=httpx.MockTransport(reject_request))
        clients.append(client)
        return client

    monkeypatch.setattr(helper, "_build_httpx_client", make_client)
    yield runtime
    for client in clients:
        if isinstance(client, httpx.AsyncClient):
            asyncio.run(client.aclose())
        else:
            client.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    ("saved", "override", "expected"),
    [
        (None, {}, None),
        (0.3, {}, 0.3),
        (0.3, {"temperature": None}, None),
        (0.3, {"temperature": 0}, 0),
        (None, {"temperature": 1}, 1),
    ],
)
def test_llm_payload_omits_empty_temperature(
    monkeypatch, offline_runtime, protocol, saved, override, expected,
) -> None:
    """两个 OpenAI 协议均省略空温度，并保留显式零及未覆盖的已保存值。"""
    original_setting = helper.get_runtime_setting
    monkeypatch.setattr(
        helper, "get_runtime_setting",
        lambda key, default=None: saved if key == "LLM_TEMPERATURE" else original_setting(key, default),
    )
    model = asyncio.run(LLMHelper.get_llm(
        provider="openai", model="gpt-6-luna", api_key="offline-key",
        base_url="https://offline.example/v1", use_proxy=False,
        api_protocol=protocol, web_search_mode="disabled", thinking_level="off",
        provider_runtime=offline_runtime, **override,
    ))

    payload = model._get_request_payload("只回复 OK")

    if expected is None:
        assert "temperature" not in payload
    else:
        assert payload["temperature"] == expected


@pytest.mark.parametrize("model_name", ["gemini-2.5-flash", "gemini-3.6-flash-preview"])
@pytest.mark.parametrize("temperature", [None, 0, 1])
def test_google_empty_temperature_uses_sdk_default(
    monkeypatch, offline_runtime, model_name, temperature,
) -> None:
    """原生 Google SDK 不接受 None，留空须保留其模型默认值且显式零仍有效。"""
    from langchain_google_genai import chat_models

    offline_runtime.resolve_runtime.return_value.update(
        provider_id="google", runtime="google", model_id=model_name, base_url=None,
    )
    monkeypatch.setattr(helper, "_patch_gemini_thought_signature", lambda: None)
    monkeypatch.setattr(chat_models, "Client", Mock(return_value=Mock(aio=None)))

    model = asyncio.run(LLMHelper.get_llm(
        provider="google", model=model_name, api_key="offline-key", use_proxy=False,
        web_search_mode="disabled", thinking_level="off", temperature=temperature,
        provider_runtime=offline_runtime,
    ))

    expected = temperature if temperature is not None else (0.7 if "2.5" in model_name else 1.0)
    assert model.temperature == expected
    assert model._prepare_params(None).temperature == expected


@pytest.mark.parametrize("override", [{}, {"temperature": None}, {"temperature": 0}])
def test_llm_test_preserves_temperature_presence(monkeypatch, override) -> None:
    """连接测试区分未传温度与显式留空，避免清空草稿沿用旧配置。"""
    get_model = AsyncMock(return_value=SimpleNamespace(
        ainvoke=AsyncMock(return_value=SimpleNamespace(content="OK")),
    ))
    monkeypatch.setattr(LLMHelper, "get_llm", get_model)

    result = asyncio.run(LLMHelper.test_current_settings(
        provider="openai", model="gpt-6-luna", **override,
    ))

    assert result["reply_preview"] == "OK"
    forwarded = get_model.await_args.kwargs
    assert ("temperature" in forwarded) == ("temperature" in override)
    if "temperature" in override:
        assert forwarded["temperature"] == override["temperature"]


@pytest.mark.parametrize("override", [{}, {"temperature": None}, {"temperature": 0}])
def test_provider_test_preserves_temperature_presence(monkeypatch, override) -> None:
    """管理 API 原样保留温度是否出现及其空值，不借用已保存温度。"""
    manager = LLMProviderManager()
    monkeypatch.setattr(manager, "get_saved_auth", lambda _provider: None)
    monkeypatch.setattr(manager, "get_provider", lambda _provider: SimpleNamespace(oauth_methods=()))
    test_settings = AsyncMock(return_value={"reply_preview": "OK"})
    monkeypatch.setattr(LLMHelper, "test_current_settings", test_settings)

    result = asyncio.run(manager.provider_manage(
        "openai", "test", enabled=True, model="gpt-6-luna", api_key="offline-key", **override,
    ))

    assert result["success"] is True
    forwarded = test_settings.await_args.kwargs
    assert ("temperature" in forwarded) == ("temperature" in override)
    if "temperature" in override:
        assert forwarded["temperature"] == override["temperature"]
