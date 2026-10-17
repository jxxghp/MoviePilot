"""
API Route 预设通过共享 OpenAI 兼容通道发现模型的边界回归。
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.llm.provider import LLMProviderManager
from app.foundation.singleton import Singleton


@pytest.fixture
def manager(monkeypatch):
    """
    隔离管理器单例及目录缓存，防止测试影响其他 provider。
    """
    key = (LLMProviderManager, (), frozenset())
    monkeypatch.setattr(Singleton, "_instances", {k: v for k, v in Singleton._instances.items() if k != key})
    instance = LLMProviderManager()
    monkeypatch.setattr(instance, "get_models_dev_data", AsyncMock(return_value={}))
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url, expected_url",
    [
        (None, "https://global.api-route.com/v1/models"),
        ("https://relay.example/v1", "https://relay.example/v1/models"),
    ],
)
async def test_api_route_discovers_authenticated_models(manager, monkeypatch, base_url, expected_url):
    """
    预设及覆盖地址都传递密钥，保留完整模型 ID，不推断缺失的上下文元数据。
    """
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-6.1-sol", "object": "model", "created": 0, "owned_by": "api-route"},
                    {"id": "other/model", "object": "model", "created": 0, "owned_by": "api-route"},
                ],
            },
        )

    monkeypatch.setattr(
        manager,
        "_build_sdk_http_client",
        lambda use_proxy: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )

    models = await manager.list_models("api-route", api_key="test-api-route-key", base_url=base_url)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == expected_url
    assert requests[0].headers["Authorization"] == "Bearer test-api-route-key"
    assert [model["id"] for model in models] == ["gpt-6.1-sol", "other/model"]
    assert all(model["source"] == "provider" and model["context_tokens"] is None for model in models)
