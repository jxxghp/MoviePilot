import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.agent.llm import helper
from app.agent.llm.helper import LLMHelper


@pytest.mark.parametrize(
    ("provider", "model", "thinking_level", "expected"),
    [
        (
            "openai",
            "deepseek-v4.1-flash",
            "high",
            {"reasoning_effort": "high"},
        ),
        (
            "openai",
            "custom-reasoning-model",
            "max",
            {"reasoning_effort": "max"},
        ),
        (
            "chatgpt",
            "claude-sonnet-4-5",
            "medium",
            {"reasoning_effort": "medium"},
        ),
    ],
)
def test_openai_compatible_models_forward_reasoning_effort(
    provider,
    model,
    thinking_level,
    expected,
):
    """未知模型目录时，OpenAI-compatible 端点应透传用户选择的思考级别。"""
    assert (
        LLMHelper._build_thinking_kwargs(
            provider=provider,
            model=model,
            thinking_level=thinking_level,
        )
        == expected
    )


def test_openai_reasoning_effort_respects_model_catalog():
    """模型目录声明的 effort 范围应约束超出范围的统一思考级别。"""
    metadata = {
        "reasoning": True,
        "reasoning_options": [
            {"type": "effort", "values": ["low", "medium", "high"]}
        ],
    }

    assert LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="catalog-model",
        thinking_level="max",
        model_metadata=metadata,
    ) == {"reasoning_effort": "high"}
    assert LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="catalog-model",
        thinking_level="off",
        model_metadata=metadata,
    ) == {}


def test_grok_max_uses_highest_catalog_effort():
    """Grok 4.6 目录最高为 xhigh，选择 max 时按已知能力降级。"""
    metadata = {
        "reasoning": True,
        "reasoning_options": [
            {"type": "effort", "values": ["low", "medium", "high", "xhigh"]}
        ],
    }

    assert LLMHelper._build_thinking_kwargs(
        provider="openai", model="grok-4.6", thinking_level="max", model_metadata=metadata,
    ) == {"reasoning_effort": "xhigh"}


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
        raise AssertionError("思考级别序列化测试不得发起外部请求")

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
@pytest.mark.parametrize("effort", ["max", "xhigh"])
@pytest.mark.parametrize("has_catalog", [False, True])
def test_openai_payload_preserves_selected_reasoning_effort(
    offline_runtime, protocol, effort, has_catalog,
) -> None:
    """SDK 实际请求须区分 max 和 xhigh，目录未知或支持两者时均保留用户选择。"""
    if has_catalog:
        offline_runtime.resolve_runtime.return_value["model_metadata"] = {
            "reasoning": True,
            "reasoning_options": [
                {"type": "effort", "values": ["none", "low", "medium", "high", "xhigh", "max"]}
            ],
        }

    model = asyncio.run(LLMHelper.get_llm(
        provider="openai", model="gpt-6-luna", api_key="offline-key",
        base_url="https://offline.example/v1", use_proxy=False,
        api_protocol=protocol, web_search_mode="disabled", thinking_level=effort,
        provider_runtime=offline_runtime,
    ))
    payload = model._get_request_payload("只回复 OK")

    if protocol == "responses":
        assert payload["reasoning"]["effort"] == effort
    else:
        assert payload["reasoning_effort"] == effort


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_grok_payload_uses_highest_catalog_effort(offline_runtime, protocol) -> None:
    """两种协议均按 Grok 4.6 的已知能力把 max 降级为 xhigh。"""
    metadata = {
        "reasoning": True,
        "reasoning_options": [
            {"type": "effort", "values": ["low", "medium", "high", "xhigh"]}
        ],
    }
    offline_runtime.resolve_runtime.return_value.update(
        model_id="grok-4.6", model_metadata=metadata,
    )

    model = asyncio.run(LLMHelper.get_llm(
        provider="openai", model="grok-4.6", api_key="offline-key",
        base_url="https://offline.example/v1", use_proxy=False,
        api_protocol=protocol, web_search_mode="disabled", thinking_level="max",
        provider_runtime=offline_runtime,
    ))
    payload = model._get_request_payload("只回复 OK")

    if protocol == "responses":
        assert payload["reasoning"]["effort"] == "xhigh"
    else:
        assert payload["reasoning_effort"] == "xhigh"


def test_openai_reasoning_effort_skips_known_unsupported_model(monkeypatch):
    """模型目录明确不支持思考时应跳过参数并记录原因。"""
    logger = Mock()
    monkeypatch.setattr("app.agent.llm.helper.logger", logger)

    result = LLMHelper._build_thinking_kwargs(
        provider="openai",
        model="chat-model",
        thinking_level="high",
        model_metadata={"reasoning": False, "reasoning_options": None},
    )

    assert result == {}
    logger.warning.assert_called_once()
