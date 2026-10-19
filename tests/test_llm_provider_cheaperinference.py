"""Cheaper Inference 预设通过共享 OpenAI 兼容通道发现模型的边界回归。"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.llm.provider import LLMProviderManager
from app.foundation.singleton import Singleton


@pytest.fixture
def manager(monkeypatch):
    """隔离管理器单例及目录缓存，防止测试影响其他 provider。"""
    key = (LLMProviderManager, (), frozenset())
    monkeypatch.setattr(Singleton, "_instances", {k: v for k, v in Singleton._instances.items() if k != key})
    instance = LLMProviderManager()
    monkeypatch.setattr(instance, "get_models_dev_data", AsyncMock(return_value={}))
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url, expected_url",
    [
        (None, "https://api.cheaperinference.com/v1/models"),
        ("https://relay.example/v1", "https://relay.example/v1/models"),
    ],
)
async def test_cheaperinference_discovers_authenticated_models(manager, monkeypatch, base_url, expected_url):
    """预设及覆盖地址都传递密钥，保留完整模型 ID，不推断缺失的上下文元数据。"""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5.4-mini", "object": "model", "created": 0, "owned_by": "cheaperinference"},
                    {"id": "other/model", "object": "model", "created": 0, "owned_by": "cheaperinference"},
                ],
            },
        )

    monkeypatch.setattr(
        manager,
        "_build_sdk_http_client",
        lambda use_proxy: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )

    models = await manager.list_models("cheaperinference", api_key="test-cheaperinference-key", base_url=base_url)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == expected_url
    assert requests[0].headers["Authorization"] == "Bearer test-cheaperinference-key"
    assert [model["id"] for model in models] == ["gpt-5.4-mini", "other/model"]
    assert all(model["source"] == "provider" and model["context_tokens"] is None for model in models)
