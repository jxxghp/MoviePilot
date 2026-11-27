"""
验证模型提供方目录、服务预设及运行时身份解析。
"""

import asyncio
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agent.llm import discovery as discovery_module
from app.agent.llm.provider import (
    LLMProviderError,
    LLMProviderManager,
    PendingAuthSession,
)
from app.foundation.singleton import Singleton

_MANAGER_SINGLETON_KEY = (LLMProviderManager, (), frozenset())


@pytest.fixture
def _isolate_provider_manager():
    """
    隔离模型提供方管理器单例，并在结束时恢复测试前的实例。
    """
    previous = Singleton._instances.pop(_MANAGER_SINGLETON_KEY, None)
    try:
        yield
    finally:
        Singleton._instances.pop(_MANAGER_SINGLETON_KEY, None)
        if previous is not None:
            Singleton._instances[_MANAGER_SINGLETON_KEY] = previous


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_models_dev_catalog_does_not_expand_unlisted_providers() -> None:
    """
    models.dev 目录仅提供模型元数据，不再向提供商列表动态扩充未内置平台。
    """
    manager = LLMProviderManager()
    provider_id = "unlisted-provider"
    manager._models_dev_data = {
        provider_id: {
            "id": provider_id,
            "name": provider_id,
            "npm": "@ai-sdk/openai-compatible",
            "env": ["UNLISTED_API_KEY"],
            "api": "https://unlisted.example.test/v1",
            "models": {},
        }
    }

    with pytest.raises(LLMProviderError):
        manager.get_provider(provider_id)
    assert provider_id not in {item["id"] for item in manager.list_providers()}
    assert provider_id not in {
        item["id"] for item in asyncio.run(manager.list_providers_async(force_refresh=True))
    }


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_provider_catalog_exposes_generic_entries_and_curated_vendors() -> None:
    """
    内置目录暴露通用兼容入口及受支持的平台。
    """
    manager = LLMProviderManager()
    providers = asyncio.run(manager.list_providers_async())
    provider_ids = [item["id"] for item in providers]

    assert provider_ids[:2] == ["openai", "anthropic-compatible"]
    for kept in ("opencode", "modelscope", "qiniu-ai", "openrouter", "siliconflow"):
        assert kept in provider_ids


@pytest.mark.usefixtures("_isolate_provider_manager")
@pytest.mark.parametrize(
    ("provider_id", "name", "base_url", "model_list_strategy"),
    [
        ("atlascloud", "Atlas Cloud", "https://api.atlascloud.ai/v1", "openai_compatible"),
        ("fireworks-ai", "Fireworks AI", "https://api.fireworks.ai/inference/v1", "models_dev_only"),
        ("huggingface", "Hugging Face", "https://router.huggingface.co/v1", "models_dev_only"),
        ("jiekou", "接口AI", "https://api.jiekou.ai/openai", "models_dev_only"),
        ("kilo", "Kilo", "https://api.kilo.ai/api/gateway", "models_dev_only"),
        ("llama", "Meta Llama", "https://api.llama.com/compat/v1", "models_dev_only"),
        ("llmgateway", "LLM Gateway", "https://api.llmgateway.io/v1", "models_dev_only"),
        ("nova", "Amazon Nova", "https://api.nova.amazon.com/v1", "models_dev_only"),
        ("poe", "Poe", "https://api.poe.com/v1", "models_dev_only"),
    ],
)
def test_retained_platforms_are_visible_and_resolve_runtime(
    provider_id: str,
    name: str,
    base_url: str,
    model_list_strategy: str,
) -> None:
    """
    保留平台作为正式目录项提供选择、模型目录和连接配置。

    :param provider_id: 正式平台标识
    :param name: 平台显示名称
    :param base_url: 平台默认服务地址
    :param model_list_strategy: 平台的模型目录读取方式
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {}
    for providers in (manager.list_providers(), asyncio.run(manager.list_providers_async())):
        entry = next(item for item in providers if item["id"] == provider_id)
        assert entry["name"] == name
        assert entry["default_base_url"] == base_url
        assert entry["supports_api_key"]

    provider = manager.get_provider(provider_id)
    assert provider.runtime == "openai_compatible"
    assert provider.models_dev_provider_id == provider_id
    assert provider.model_list_strategy == model_list_strategy
    assert manager.resolve_model_list_base_url(provider_id, None) == base_url

    runtime = asyncio.run(manager.resolve_runtime(
        provider_id=provider_id,
        model="provider-model",
        api_key="offline-key",
    ))
    assert runtime["provider_id"] == provider_id
    assert runtime["runtime"] == "openai_compatible"
    assert runtime["base_url"] == base_url
    assert runtime["api_key"] == "offline-key"
    assert runtime["model_id"] == "provider-model"


@pytest.mark.usefixtures("_isolate_provider_manager")
@pytest.mark.parametrize("base_url", [None, "https://custom.example.test/v1"])
def test_unknown_provider_uses_standard_resolution_error(
    base_url: Optional[str],
) -> None:
    """
    未定义的提供商通过统一解析逻辑返回错误，不受自定义地址影响。
    """
    manager = LLMProviderManager()
    provider_id = "unknown-provider"

    with pytest.raises(LLMProviderError, match=f"不支持的 LLM 提供商：{provider_id}"):
        asyncio.run(manager.resolve_runtime(
            provider_id=provider_id,
            model="custom-model",
            api_key="test-key",
            base_url=base_url,
        ))
    with pytest.raises(LLMProviderError, match=f"不支持的 LLM 提供商：{provider_id}"):
        manager.resolve_model_list_base_url(provider_id, base_url)


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_generic_anthropic_compatible_provider_runtime_and_metadata_boundary() -> None:
    """
    通用 Anthropic 兼容入口要求填写地址、规范化 /v1 路径且不信任官方上限元数据。
    """
    manager = LLMProviderManager()
    provider = manager.get_provider("anthropic-compatible")

    assert provider.name == "Anthropic 兼容"
    assert provider.runtime == "anthropic_compatible"
    assert provider.base_url_editable
    assert provider.requires_base_url
    assert provider.supports_model_refresh
    assert provider.model_list_strategy == "anthropic_endpoint"

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="anthropic-compatible",
            model="claude-sonnet-4-6",
            api_key="sk-ant-relay",
            base_url="https://relay.example.com/anthropic/v1",
        )
    )

    assert runtime["runtime"] == "anthropic_compatible"
    assert runtime["base_url"] == "https://relay.example.com/anthropic"
    assert not runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
@pytest.mark.parametrize(
    ("input_base_url", "expected_models_url"),
    [
        ("https://relay.example.com", "https://relay.example.com/v1/models"),
        ("https://relay.example.com/v1", "https://relay.example.com/v1/models"),
        ("https://relay.example.com/anthropic/v1", "https://relay.example.com/anthropic/v1/models"),
    ],
)
def test_generic_anthropic_compatible_lists_models_from_endpoint(
    input_base_url: str,
    expected_models_url: str,
) -> None:
    """
    通用 Anthropic 兼容入口按标准 Anthropic 协议头请求目标 `/v1/models` 刷新模型。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {}
    captured: dict[str, object] = {}

    class _FakeResponse:
        """
        模拟 Anthropic `/v1/models` JSON 响应。
        """

        def raise_for_status(self) -> None:
            """
            模拟 HTTP 200 状态校验。
            """

        def json(self) -> dict[str, object]:
            """
            返回包含标准与自定义模型的列表载荷。
            """
            return {
                "data": [
                    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
                    {"id": "custom-relay-model"},
                ]
            }

    class _FakeAsyncRequest:
        """
        捕获目标 URL 与请求头的最小异步请求替身。
        """

        def __init__(self, headers: dict[str, str]) -> None:
            """
            记录构造时传入的请求头。
            """
            self.headers = headers

        async def get_res(
            self, url: str, params: dict[str, object] | None = None, raise_exception: bool = False,
        ) -> _FakeResponse:
            """
            记录请求参数并返回测试模型列表。
            """
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = dict(self.headers)
            captured["raise_exception"] = raise_exception
            return _FakeResponse()

    def _fake_build_request(use_proxy=None, headers=None):
        """
        返回捕获请求头的异步请求替身。
        """
        del use_proxy
        return _FakeAsyncRequest(headers or {})

    with patch.object(manager, "_build_async_request", side_effect=_fake_build_request):
        models = asyncio.run(
            manager.list_models(
                provider_id="anthropic-compatible",
                api_key="sk-ant-test",
                base_url=input_base_url,
                user_agent="CustomAgent/1.0",
            )
        )

    assert captured["url"] == expected_models_url
    assert captured["params"] is None
    assert captured["raise_exception"] is True
    assert captured["headers"] == {
        "User-Agent": "CustomAgent/1.0",
        "x-api-key": "sk-ant-test",
        "anthropic-version": "2023-06-01",
    }
    assert [(item["id"], item["name"], item["transport"], item["source"]) for item in models] == [
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", "anthropic", "provider"),
        ("custom-relay-model", "custom-relay-model", "anthropic", "provider"),
    ]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_generic_anthropic_compatible_skips_invalid_items_and_rejects_bad_payload() -> None:
    """
    通用 Anthropic 兼容端点刷新模型时过滤畸形条目，并在非列表响应或空地址时报错。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {}
    payloads: list[dict[str, object]] = [
        {"data": ["bad-item", {"id": ""}, {"id": "claude-haiku-4-5"}]},
        {"data": "invalid"},
    ]

    class _FakeResponse:
        """
        依次返回预置响应体的最小 HTTP 响应替身。
        """

        def __init__(self, body: dict[str, object]) -> None:
            """
            记录本次响应体。
            """
            self._body = body

        def raise_for_status(self) -> None:
            """
            模拟 HTTP 200 状态校验。
            """

        def json(self) -> dict[str, object]:
            """
            返回本次预置 JSON 载荷。
            """
            return self._body

    class _FakeAsyncRequest:
        """
        顺序返回预置模型列表响应的异步请求替身。
        """

        async def get_res(
            self, url: str, params: dict[str, object] | None = None, raise_exception: bool = False,
        ) -> _FakeResponse:
            """
            消费下一份测试载荷。
            """
            del url, params, raise_exception
            return _FakeResponse(payloads.pop(0))

    with patch.object(manager, "_build_async_request", return_value=_FakeAsyncRequest()):
        models = asyncio.run(
            manager.list_models(
                provider_id="anthropic-compatible",
                api_key="sk-ant-test",
                base_url="https://relay.example.com/v1",
            )
        )
        assert [item["id"] for item in models] == ["claude-haiku-4-5"]

        with pytest.raises(LLMProviderError, match="响应格式不正确"):
            asyncio.run(
                manager.list_models(
                    provider_id="anthropic-compatible",
                    api_key="sk-ant-test",
                    base_url="https://relay.example.com/v1",
                )
            )

    with pytest.raises(LLMProviderError, match="缺少有效的 Base URL"):
        asyncio.run(
            manager._list_models_from_anthropic_endpoint(
                provider_id="anthropic-compatible",
                api_key="sk-ant-test",
                base_url="   ",
            )
        )


class _PagedAnthropicResponse:
    """
    返回预置分页载荷的最小 HTTP 响应替身。
    """

    def __init__(self, body: dict[str, object]) -> None:
        """
        记录本页响应体。

        :param body: 本页 JSON 载荷
        """
        self._body = body

    def raise_for_status(self) -> None:
        """
        模拟 HTTP 200 状态校验。
        """

    def json(self) -> dict[str, object]:
        """
        返回本页 JSON 载荷。
        """
        return self._body


class _PagedAnthropicRequest:
    """
    按调用顺序返回分页载荷并记录每次查询参数的异步请求替身。
    """

    def __init__(self, pages: list[dict[str, object]]) -> None:
        """
        保存待返回的分页载荷。

        :param pages: 按请求顺序返回的响应体；用尽后重复返回最后一页
        """
        self._pages = pages
        self.params: list[dict[str, object] | None] = []

    async def get_res(
        self, url: str, params: dict[str, object] | None = None, raise_exception: bool = False,
    ) -> _PagedAnthropicResponse:
        """
        记录本次查询参数并返回下一页。

        :param url: 模型列表接口地址
        :param params: 本次分页查询参数
        :param raise_exception: 是否要求透传请求异常
        :return: 下一页响应替身
        """
        del url, raise_exception
        self.params.append(params)
        return _PagedAnthropicResponse(self._pages[min(len(self.params), len(self._pages)) - 1])


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_generic_anthropic_compatible_follows_model_list_cursor() -> None:
    """
    Anthropic 兼容端点声明 has_more 时按 last_id 继续翻页，跨页重复的模型只保留一次。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {}
    request = _PagedAnthropicRequest(
        [
            {
                "data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}],
                "has_more": True,
                "first_id": "claude-opus-5-5",
                "last_id": "claude-sonnet-5-5",
            },
            {
                "data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-4-5"}],
                "has_more": False,
                "first_id": "claude-sonnet-5-5",
                "last_id": "claude-haiku-4-5",
            },
        ]
    )

    with patch.object(manager, "_build_async_request", return_value=request):
        models = asyncio.run(
            manager.list_models(
                provider_id="anthropic-compatible",
                api_key="sk-ant-test",
                base_url="https://relay.example.com/v1",
            )
        )

    assert request.params == [None, {"limit": 1000, "after_id": "claude-sonnet-5-5"}]
    assert sorted(item["id"] for item in models) == [
        "claude-haiku-4-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]


@pytest.mark.parametrize(
    ("pages", "expected_requests"),
    [
        pytest.param(
            [{"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"}],
            2,
            id="repeated-cursor",
        ),
        pytest.param(
            [{"data": [{"id": "claude-a"}], "has_more": True}],
            1,
            id="missing-cursor",
        ),
        pytest.param(
            [
                {"data": [{"id": f"claude-{index}"}], "has_more": True, "last_id": f"claude-{index}"}
                for index in range(5)
            ],
            3,
            id="page-limit",
        ),
    ],
)
@pytest.mark.usefixtures("_isolate_provider_manager")
def test_generic_anthropic_compatible_stops_unbounded_pagination(
    monkeypatch, pages: list[dict[str, object]], expected_requests: int,
) -> None:
    """
    兼容端点的游标缺失、重复或页数超限时停止翻页，保留已读取的模型并提示列表可能不完整。
    """
    monkeypatch.setattr(discovery_module, "_ANTHROPIC_MODELS_MAX_PAGES", 3)
    manager = LLMProviderManager()
    manager._models_dev_data = {}
    request = _PagedAnthropicRequest(pages)

    with patch.object(manager, "_build_async_request", return_value=request), patch.object(
        discovery_module.logger, "warning",
    ) as warning:
        models = asyncio.run(
            manager.list_models(
                provider_id="anthropic-compatible",
                api_key="sk-ant-test",
                base_url="https://relay.example.com/v1",
            )
        )

    assert len(request.params) == expected_requests
    assert models
    assert request.params[0] is None
    assert any("可能不完整" in str(call.args[0]) for call in warning.call_args_list)


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_generic_openai_compatible_lists_models_via_authenticated_endpoint() -> None:
    """
    通用 OpenAI 兼容入口通过目标 `/models` 端点携带密钥刷新模型列表。
    """
    manager = LLMProviderManager()
    requests: list[httpx.Request] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        """
        记录发往兼容端点的模型目录请求并返回标准列表响应。
        """
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5.4", "object": "model", "created": 0},
                    {"id": "custom-openai-model", "object": "model", "created": 0},
                ],
            },
        )

    with patch.object(
        manager,
        "_build_sdk_http_client",
        side_effect=lambda use_proxy=None: httpx.AsyncClient(transport=httpx.MockTransport(_respond)),
    ):
        models = asyncio.run(
            manager.list_models(
                provider_id="openai",
                api_key="sk-openai-relay",
                base_url="https://relay.example.com/v1",
            )
        )

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://relay.example.com/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer sk-openai-relay"
    assert [(item["id"], item["source"], item["context_tokens"]) for item in models] == [
        ("custom-openai-model", "provider", None),
        ("gpt-5.4", "provider", None),
    ]


@pytest.mark.usefixtures("_isolate_provider_manager")
@pytest.mark.parametrize(
    "provider_id",
    ["modelscope", "fireworks-ai", "huggingface", "jiekou", "kilo", "llama", "llmgateway", "nova", "poe"],
)
def test_list_models_for_catalog_builtin_uses_models_dev_after_refresh(provider_id: str) -> None:
    """
    保留的目录型内置提供方在刷新后仍从 models.dev 读取模型列表。

    :param provider_id: 使用元数据目录读取模型的正式平台标识
    """
    manager = LLMProviderManager()
    payload = {
        provider_id: {
            "id": provider_id,
            "name": provider_id,
            "models": {
                "provider-model": {
                    "name": "Provider Model",
                    "limit": {"context": 131072},
                }
            },
        }
    }

    async def _load_models_dev(force_refresh: bool = False, use_proxy=None):
        """
        返回测试预设的模型目录，避免真实目录请求。
        """
        del force_refresh, use_proxy
        manager._models_dev_data = payload
        return payload

    with patch.object(manager, "get_models_dev_data", AsyncMock(side_effect=_load_models_dev)):
        models = asyncio.run(
            manager.list_models(
                provider_id=provider_id,
                api_key="sk-test",
                force_refresh=True,
            )
        )

    assert [item["id"] for item in models] == ["provider-model"]
    assert models[0]["source"] == "models.dev"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_get_models_dev_data_falls_back_to_bundled_file_when_fetch_and_cache_fail() -> None:
    """
    网络读取与缓存都失败时回退到随包附带的模型目录。
    """
    manager = LLMProviderManager()
    payload = {
        "qiniu-ai": {
            "id": "qiniu-ai",
            "name": "Qiniu",
            "models": {},
        }
    }

    with (
        patch.object(manager, "_fetch_models_dev", AsyncMock(side_effect=RuntimeError("offline"))),
        patch.object(manager, "_load_models_dev_from_disk", AsyncMock(return_value=None)),
        patch.object(manager, "_load_bundled_models_dev_payload", return_value=payload),
    ):
        data = asyncio.run(manager.get_models_dev_data(force_refresh=True))

    assert data == payload
    assert manager._models_dev_data == payload


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_cached_models_dev_payload_falls_back_to_bundled_file() -> None:
    """
    缓存载荷不可用时读取随包附带的模型目录用于查询内置提供方模型元数据。
    """
    manager = LLMProviderManager()
    payload = {
        "qiniu-ai": {
            "id": "qiniu-ai",
            "name": "Qiniu",
            "models": {
                "deepseek-r1": {
                    "name": "DeepSeek R1",
                    "limit": {"context": 65536},
                }
            },
        }
    }

    missing_cache_path = Path(f"/tmp/llm-provider-cache-missing-{id(manager)}.json")

    with (
        patch.object(manager, "_models_dev_cache_path", missing_cache_path),
        patch.object(manager, "_load_bundled_models_dev_payload", return_value=payload),
    ):
        metadata = manager.resolve_cached_model_metadata("qiniu-ai", "deepseek-r1")

    assert metadata is not None
    assert metadata["name"] == "DeepSeek R1"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_provider_includes_baidu_qianfan_base_url_presets() -> None:
    """
    百度千帆提供方包含可选择的基础地址预设。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("baidu-qianfan-coding-plan")

    assert provider.name == "百度千帆"
    assert provider.runtime == "openai_compatible"
    assert provider.default_base_url == "https://qianfan.baidubce.com/v2"
    assert tuple(((preset.label, preset.value) for preset in provider.base_url_presets)) == (
        ("通用 API", "https://qianfan.baidubce.com/v2"),
        ("Coding Plan", "https://qianfan.baidubce.com/v2/coding"),
    )
    assert provider.models_dev_provider_id is None
    assert not provider.supports_model_refresh


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_provider_includes_jdcloud_base_url_presets() -> None:
    """
    京东云提供方包含可选择的基础地址预设。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("jdcloud")

    assert provider.name == "京东云"
    assert provider.runtime == "openai_compatible"
    assert provider.default_base_url == "https://modelservice.jdcloud.com/v1"
    assert tuple(((preset.label, preset.value) for preset in provider.base_url_presets)) == (
        ("通用 API", "https://modelservice.jdcloud.com/v1"),
        ("Coding Plan", "https://modelservice.jdcloud.com/coding/openai/v1"),
    )
    assert provider.models_dev_provider_id is None
    assert not provider.supports_model_refresh


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_provider_includes_kuaishou_wanqing_endpoint() -> None:
    """
    快手万擎提供方包含预期服务端点。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("kuaishou-wanqing")

    assert provider.name == "快手万擎"
    assert provider.runtime == "openai_compatible"
    assert provider.default_base_url == "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints"
    assert tuple(((preset.id, preset.label, preset.value) for preset in provider.base_url_presets)) == (
        ("kuaishou-wanqing-usage", "按量计费", "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints"),
        ("kuaishou-wanqing-coding", "Coding Plan", "https://wanqing.streamlakeapi.com/api/gateway/coding/v1"),
    )
    assert provider.model_list_strategy == "manual"
    assert not provider.supports_model_refresh


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_kuaishou_wanqing_coding_preset_resolves_runtime_base_url() -> None:
    """
    快手万擎编程预设解析到正确的运行时基础地址。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="kuaishou-wanqing",
            model="kat-coder-pro-v2",
            api_key="sk-test",
            base_url="https://wanqing.streamlakeapi.com/api/gateway/coding/v1",
            base_url_preset_id="kuaishou-wanqing-coding",
        )
    )

    assert runtime["runtime"] == "openai_compatible"
    assert runtime["base_url"] == "https://wanqing.streamlakeapi.com/api/gateway/coding/v1"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_kuaishou_wanqing_models_are_manual_input() -> None:
    """
    快手万擎模型使用手动输入策略。
    """
    manager = LLMProviderManager()

    models = asyncio.run(manager.list_models(provider_id="kuaishou-wanqing"))

    assert models == []


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_provider_includes_china_operator_token_services() -> None:
    """
    三大运营商 Token 服务应作为内置 OpenAI-compatible provider 暴露。
    """
    manager = LLMProviderManager()

    unicom = manager.get_provider("china-unicom")
    mobile = manager.get_provider("china-mobile")
    telecom = manager.get_provider("china-telecom")

    assert unicom.name == "中国联通"
    assert unicom.default_base_url == "https://aigw-gzgy2.cucloud.cn:8443/v1"
    assert tuple(((preset.id, preset.value, preset.runtime) for preset in unicom.base_url_presets)) == (
        ("china-unicom-coding-openai", "https://aigw-gzgy2.cucloud.cn:8443/v1", None),
        ("china-unicom-coding-anthropic", "https://aigw-gzgy2.cucloud.cn:8443", "anthropic_compatible"),
    )
    assert unicom.base_url_editable
    assert not unicom.supports_model_refresh
    assert unicom.model_list_strategy == "manual"

    assert mobile.name == "中国移动"
    assert mobile.default_base_url == "https://ecloud.10086.cn/api"
    assert tuple(((preset.id, preset.value) for preset in mobile.base_url_presets)) == (
        ("china-mobile-moma", "https://ecloud.10086.cn/api"),
        ("china-mobile-coding", "https://zhenze-huhehaote.cmecloud.cn/api/coding/v1"),
    )
    assert mobile.base_url_editable
    assert not mobile.supports_model_refresh
    assert mobile.model_list_strategy == "manual"

    assert telecom.name == "中国电信"
    assert telecom.default_base_url == "https://wishub-x6.ctyun.cn/v1"
    assert telecom.api_key_label == "App Key"
    assert tuple(
        ((preset.id, preset.value, preset.runtime, preset.model_list_strategy) for preset in telecom.base_url_presets)
    ) == (
        ("china-telecom-token-service", "https://wishub-x6.ctyun.cn/v1", None, None),
        ("china-telecom-coding-openai", "https://wishub-x6.ctyun.cn/coding/v1", None, "manual"),
        ("china-telecom-coding-anthropic", "https://wishub-x6.ctyun.cn/coding/v1", "anthropic_compatible", "manual"),
    )
    assert telecom.base_url_editable


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_china_operator_manual_model_presets_return_empty_model_list() -> None:
    """
    未提供稳定全局模型目录的运营商套餐应回退为手动填写模型。
    """
    manager = LLMProviderManager()

    unicom_models = asyncio.run(manager.list_models(provider_id="china-unicom"))
    mobile_models = asyncio.run(manager.list_models(provider_id="china-mobile"))
    telecom_coding_models = asyncio.run(
        manager.list_models(
            provider_id="china-telecom",
            base_url_preset_id="china-telecom-coding-openai",
        )
    )

    assert unicom_models == []
    assert mobile_models == []
    assert telecom_coding_models == []


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_china_operator_anthropic_presets_resolve_runtime() -> None:
    """
    运营商提供 Anthropic 协议地址时应切换到 anthropic_compatible runtime。
    """
    manager = LLMProviderManager()

    unicom_runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="china-unicom",
            model=None,
            api_key="sk-test",
            base_url="https://aigw-gzgy2.cucloud.cn:8443",
            base_url_preset_id="china-unicom-coding-anthropic",
        )
    )
    telecom_runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="china-telecom",
            model=None,
            api_key="cp-test",
            base_url="https://wishub-x6.ctyun.cn/coding/v1",
            base_url_preset_id="china-telecom-coding-anthropic",
        )
    )

    assert unicom_runtime["runtime"] == "anthropic_compatible"
    assert unicom_runtime["base_url"] == "https://aigw-gzgy2.cucloud.cn:8443"
    assert telecom_runtime["runtime"] == "anthropic_compatible"
    assert telecom_runtime["base_url"] == "https://wishub-x6.ctyun.cn/coding"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_minimax_provider_merges_general_and_coding_presets() -> None:
    """
    MiniMax 提供方合并通用与编程服务预设。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("minimax")
    serialized = manager.list_providers()
    minimax_payload = next(item for item in serialized if item["id"] == "minimax")

    assert provider.name == "MiniMax"
    assert provider.runtime == "anthropic_compatible"
    assert tuple(((preset.id, preset.label, preset.value) for preset in provider.base_url_presets)) == (
        ("minimax-cn-general", "中国内地 / 通用", "https://api.minimaxi.com/anthropic/v1"),
        ("minimax-global-general", "国际站 / 通用", "https://api.minimax.io/anthropic/v1"),
        ("minimax-cn-coding", "中国内地 / Coding Plan", "https://api.minimaxi.com/anthropic/v1"),
        ("minimax-global-coding", "国际站 / Coding Plan", "https://api.minimax.io/anthropic/v1"),
    )
    assert tuple((item["id"] for item in minimax_payload["base_url_presets"])) == (
        "minimax-cn-general",
        "minimax-global-general",
        "minimax-cn-coding",
        "minimax-global-coding",
    )


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_minimax_coding_alias_resolves_to_minimax_provider() -> None:
    """
    MiniMax 编程别名解析到统一提供方。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("minimax-coding")

    assert provider.id == "minimax"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_models_dev_provider_id_prefers_minimax_preset_id() -> None:
    """
    模型目录查询优先采用 MiniMax 预设指定的提供方标识。
    """
    manager = LLMProviderManager()
    provider = manager.get_provider("minimax")

    assert (
        manager._resolve_provider_models_dev_provider_id(
            provider, base_url="https://api.minimaxi.com/anthropic/v1", base_url_preset_id="minimax-cn-coding"
        )
        == "minimax-cn-coding-plan"
    )


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_builtin_moonshot_provider_includes_kimi_for_coding_preset() -> None:
    """
    Moonshot 提供方包含 Kimi 编程服务预设。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("moonshot")
    serialized = manager.list_providers()
    moonshot_payload = next(item for item in serialized if item["id"] == "moonshot")

    assert provider.name == "Moonshot / Kimi"
    assert provider.runtime == "openai_compatible"
    assert tuple(((preset.id, preset.label, preset.value, preset.runtime) for preset in provider.base_url_presets)) == (
        ("moonshot-cn", "中国站", "https://api.moonshot.cn/v1", None),
        ("moonshot-global", "国际站", "https://api.moonshot.ai/v1", None),
        ("moonshot-kimi-coding", "Kimi for Coding", "https://api.kimi.com/coding/v1", "anthropic_compatible"),
    )
    assert tuple((item["id"] for item in moonshot_payload["base_url_presets"])) == (
        "moonshot-cn",
        "moonshot-global",
        "moonshot-kimi-coding",
    )


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_kimi_coding_alias_resolves_to_moonshot_provider() -> None:
    """
    Kimi 编程别名解析到 Moonshot 提供方。
    """
    manager = LLMProviderManager()

    provider = manager.get_provider("kimi-coding")

    assert provider.id == "moonshot"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_prefers_kimi_for_coding_preset_runtime() -> None:
    """
    Kimi 编程预设优先选择其专用运行时。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="moonshot",
            model=None,
            api_key="sk-test",
            base_url="https://api.kimi.com/coding/v1",
            base_url_preset_id="moonshot-kimi-coding",
        )
    )

    assert runtime["provider_id"] == "moonshot"
    assert runtime["runtime"] == "anthropic_compatible"
    assert runtime["base_url"] == "https://api.kimi.com/coding"
    assert runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_marks_unmatched_custom_endpoint_for_profile_cap() -> None:
    """
    自定义端点未匹配预设时标记能力配置的限制边界。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="deepseek",
            model="deepseek-chat",
            api_key="sk-test",
            base_url="https://proxy.example.com/v1",
        )
    )

    assert not runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_rejects_preset_identity_for_different_url() -> None:
    """
    基础地址不同于预设时不沿用预设身份。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="moonshot",
            model="kimi-k2.5",
            api_key="sk-test",
            base_url="https://proxy.example.com/v1",
            base_url_preset_id="moonshot-cn",
        )
    )

    assert not runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_rejects_preset_metadata_when_url_falls_back() -> None:
    """
    地址回退后不继续采用不匹配预设的元数据。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="moonshot",
            model="kimi-k2.5",
            api_key="sk-test",
            base_url_preset_id="moonshot-kimi-coding",
        )
    )

    assert not runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_never_trusts_generic_openai_model_metadata() -> None:
    """
    通用 OpenAI 兼容配置不直接信任其他来源的模型元数据。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="openai",
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
        )
    )

    assert not runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_runtime_matches_native_provider_without_base_url() -> None:
    """
    原生提供方未填写基础地址时仍能匹配运行时。
    """
    manager = LLMProviderManager()

    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="google",
            model="gemini-2.5-flash",
            api_key="sk-test",
        )
    )

    assert runtime["model_profile_endpoint_matched"]


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_resolve_model_list_strategy_prefers_kimi_for_coding_preset() -> None:
    """
    模型列表策略优先采用 Kimi 编程预设定义。
    """
    manager = LLMProviderManager()
    provider = manager.get_provider("moonshot")

    assert (
        manager._resolve_provider_model_list_strategy(
            provider, base_url="https://api.kimi.com/coding/v1", base_url_preset_id="moonshot-kimi-coding"
        )
        == "anthropic_compatible"
    )


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_chatgpt_oauth_models_follow_models_dev_catalog() -> None:
    """
    ChatGPT OAuth 模型列表跟随模型目录数据。
    """
    manager = LLMProviderManager()
    payload = {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "models": {
                "gpt-5.5": {
                    "name": "GPT-5.5",
                    "limit": {"context": 400000},
                },
                "o4-mini": {
                    "name": "o4-mini",
                    "limit": {"context": 200000},
                },
            },
        }
    }

    with patch.object(manager, "get_models_dev_data", AsyncMock(return_value=payload)):
        models = asyncio.run(manager._list_chatgpt_oauth_models(provider_id="chatgpt"))

    assert [item["id"] for item in models] == ["gpt-5.5", "o4-mini"]
    assert all((item["source"] == "models.dev" for item in models))


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_chatgpt_oauth_models_return_empty_when_catalog_missing() -> None:
    """
    缺少模型目录时 ChatGPT OAuth 返回空模型列表。
    """
    manager = LLMProviderManager()

    with patch.object(manager, "get_models_dev_data", AsyncMock(return_value={})):
        models = asyncio.run(manager._list_chatgpt_oauth_models(provider_id="chatgpt"))

    assert models == []


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_expired_auth_session_cleanup_removes_state_index() -> None:
    """
    过期授权会话应同时移除 session 与 OAuth state 索引。
    """
    manager = LLMProviderManager()
    manager._pending_sessions["session-old"] = PendingAuthSession(
        session_id="session-old",
        provider_id="chatgpt",
        method_id="browser_oauth",
        flow_type="oauth",
        expires_at=100,
    )
    manager._oauth_state_index["state-old"] = "session-old"

    with manager._lock:
        manager._cleanup_auth_sessions_locked(now=101)

    assert "session-old" not in manager._pending_sessions
    assert "state-old" not in manager._oauth_state_index
