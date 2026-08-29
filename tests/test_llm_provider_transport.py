import asyncio
from types import SimpleNamespace

from google import genai

from app.adapters.network.http import AsyncRequestUtils
from app.agent.llm import discovery as provider_module
from app.agent.llm.provider import LLMProviderManager


def test_provider_builds_secure_requestutils_without_environment_proxy(
    monkeypatch,
) -> None:
    """LLM 普通 HTTP 应走统一异步客户端并保持显式 TLS 与代理策略。"""
    settings = {
        "LLM_USE_PROXY": True,
        "PROXY_HOST": "http://proxy.example:7890",
    }
    monkeypatch.setattr(
        provider_module,
        "get_runtime_setting",
        lambda name: settings.get(name),
    )
    manager = object.__new__(LLMProviderManager)

    request_utils = manager._build_async_request()

    assert isinstance(request_utils, AsyncRequestUtils)
    assert request_utils._proxies == "http://proxy.example:7890"
    assert request_utils._verify is True
    assert request_utils._trust_env is False


def test_provider_delegates_sdk_http_client_creation(monkeypatch) -> None:
    """第三方 SDK transport 必须由统一网络适配器工厂构造。"""
    marker = object()
    captured = {}
    monkeypatch.setattr(
        provider_module,
        "get_runtime_setting",
        lambda name: {
            "LLM_USE_PROXY": True,
            "PROXY_HOST": "http://proxy.example:7890",
        }.get(name),
    )

    def create_sdk_client(**kwargs):
        """记录 SDK 客户端构造参数。"""
        captured.update(kwargs)
        return marker

    monkeypatch.setattr(
        AsyncRequestUtils,
        "create_sdk_client",
        create_sdk_client,
    )
    manager = object.__new__(LLMProviderManager)

    assert manager._build_sdk_http_client() is marker
    assert captured == {
        "proxy": "http://proxy.example:7890",
        "timeout": 15,
        "connect_timeout": 10,
        "trust_env": False,
    }


def test_google_model_client_uses_adapter_args_and_closes_both_clients(
    monkeypatch,
) -> None:
    """Google 模型目录必须复用统一 transport 参数并释放同步、异步 client。"""
    captured = {}

    class FakeAsyncModels:
        """返回固定 Google 模型目录。"""

        async def list(self):
            """返回只含一个可生成内容模型的响应。"""
            return SimpleNamespace(
                page=[
                    SimpleNamespace(
                        name="gemini-test",
                        display_name="Gemini Test",
                        supported_actions=["generateContent"],
                    )
                ]
            )

    class FakeAsyncClient:
        """记录 Google 异步 client 的关闭次数。"""

        def __init__(self) -> None:
            """初始化模型端点与关闭计数。"""
            self.models = FakeAsyncModels()
            self.close_count = 0

        async def aclose(self) -> None:
            """记录异步 client 关闭。"""
            self.close_count += 1

    class FakeClient:
        """记录 Google SDK 构造参数与同步 client 关闭次数。"""

        def __init__(self, **kwargs) -> None:
            """保存 SDK 参数并创建对应异步 client。"""
            captured.update(kwargs)
            self.aio = FakeAsyncClient()
            self.close_count = 0
            captured["client"] = self

        def close(self) -> None:
            """记录同步 client 关闭。"""
            self.close_count += 1

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(
        provider_module,
        "get_runtime_setting",
        lambda name: {
            "LLM_USE_PROXY": True,
            "PROXY_HOST": "http://proxy.example:7890",
        }.get(name),
    )
    manager = object.__new__(LLMProviderManager)

    async def fake_models_dev_model(*_args, **_kwargs):
        """避免测试访问外部 models.dev。"""
        return {}

    manager._models_dev_model = fake_models_dev_model

    result = asyncio.run(
        manager._list_models_from_google(api_key="test-key", use_proxy=True)
    )

    client = captured["client"]
    http_options = captured["http_options"]
    assert result[0]["id"] == "gemini-test"
    assert http_options.client_args["trust_env"] is False
    assert http_options.async_client_args["trust_env"] is False
    assert any(
        key in http_options.async_client_args for key in ("proxy", "proxies")
    )
    assert client.aio.close_count == 1
    assert client.close_count == 1
