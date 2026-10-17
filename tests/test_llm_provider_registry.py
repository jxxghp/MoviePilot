"""
验证模型提供方目录、服务预设及运行时身份解析。
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
def test_dynamic_provider_is_exposed_from_models_dev_cache() -> None:
    """
    模型目录缓存中的动态提供方可转换为公开定义。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "frogbot": {
            "id": "frogbot",
            "name": "FrogBot",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["FROGBOT_API_KEY"],
            "api": "https://app.frogbot.ai/api/v1",
            "models": {},
        }
    }

    provider = manager.get_provider("frogbot")

    assert provider.id == "frogbot"
    assert provider.runtime == "openai_compatible"
    assert provider.default_base_url == "https://app.frogbot.ai/api/v1"
    assert not provider.requires_base_url
    assert provider.base_url_editable
    assert provider.model_list_strategy == "models_dev_only"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_dynamic_provider_override_normalizes_chat_endpoint_base_url() -> None:
    """
    动态提供方的聊天端点覆盖值规范化为基础地址。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "bailing": {
            "id": "bailing",
            "name": "Bailing",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["BAILING_API_TOKEN"],
            "api": "https://api.tbox.cn/api/llm/v1/chat/completions",
            "models": {},
        }
    }

    provider = manager.get_provider("bailing")

    assert provider.default_base_url == "https://api.tbox.cn/api/llm/v1"
    assert provider.api_key_label == "API Token"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_dynamic_provider_skips_alias_only_models_dev_ids() -> None:
    """
    仅作为别名存在的模型目录标识不单独暴露为提供方。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "moonshotai": {
            "id": "moonshotai",
            "name": "Moonshot AI Intl",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["MOONSHOT_API_KEY"],
            "api": "https://api.moonshot.ai/v1",
            "models": {},
        }
    }

    with pytest.raises(LLMProviderError):
        manager.get_provider("moonshotai")


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_dynamic_provider_skips_incompatible_models_dev_provider() -> None:
    """
    跳过运行时不兼容的动态提供方。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "azure": {
            "id": "azure",
            "name": "Azure",
            "npm": "@ai-sdk/azure",
            "env": ["AZURE_API_KEY"],
            "models": {},
        }
    }

    with pytest.raises(LLMProviderError):
        manager.get_provider("azure")


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_dynamic_provider_without_known_base_url_requires_manual_input() -> None:
    """
    动态提供方没有已知基础地址时要求用户手动填写。
    """
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "custom-anthropic": {
            "id": "custom-anthropic",
            "name": "Custom Anthropic",
            "npm": "@ai-sdk/anthropic",
            "env": ["CUSTOM_ANTHROPIC_KEY"],
            "models": {},
        }
    }

    provider = manager.get_provider("custom-anthropic")

    assert provider.runtime == "anthropic_compatible"
    assert provider.requires_base_url
    assert provider.base_url_editable
    assert provider.model_list_strategy == "anthropic_compatible"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_list_providers_async_loads_models_dev_before_serializing() -> None:
    """
    异步列出提供方前先加载模型目录，保证序列化结果完整。
    """
    manager = LLMProviderManager()
    payload = {
        "frogbot": {
            "id": "frogbot",
            "name": "FrogBot",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["FROGBOT_API_KEY"],
            "api": "https://app.frogbot.ai/api/v1",
            "models": {},
        }
    }

    with patch.object(
        manager,
        "get_models_dev_data",
        AsyncMock(
            side_effect=lambda force_refresh=False, use_proxy=None: (
                manager.__dict__.update({"_models_dev_data": payload}) or payload
            )
        ),
    ) as fetch_mock:
        providers = asyncio.run(manager.list_providers_async())

    fetch_mock.assert_awaited_once_with(force_refresh=False, use_proxy=None)
    assert "frogbot" in {item["id"] for item in providers}


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_list_models_uses_dynamic_provider_after_refresh() -> None:
    """
    刷新模型目录后可按新发现的动态提供方列出模型。
    """
    manager = LLMProviderManager()
    payload = {
        "frogbot": {
            "id": "frogbot",
            "name": "FrogBot",
            "npm": "@ai-sdk/openai-compatible",
            "env": ["FROGBOT_API_KEY"],
            "api": "https://app.frogbot.ai/api/v1",
            "models": {
                "frog-1": {
                    "name": "Frog 1",
                    "limit": {"context": 131072},
                }
            },
        }
    }

    async def _load_models_dev(force_refresh: bool = False, use_proxy=None):
        """
        返回测试预设的动态模型目录，避免真实目录请求。
        """
        manager._models_dev_data = payload
        return payload

    with patch.object(manager, "get_models_dev_data", AsyncMock(side_effect=_load_models_dev)):
        models = asyncio.run(
            manager.list_models(
                provider_id="frogbot",
                api_key="sk-test",
                force_refresh=True,
            )
        )

    assert [item["id"] for item in models] == ["frog-1"]
    assert models[0]["source"] == "models.dev"


@pytest.mark.usefixtures("_isolate_provider_manager")
def test_get_models_dev_data_falls_back_to_bundled_file_when_fetch_and_cache_fail() -> None:
    """
    网络读取与缓存都失败时回退到随包附带的模型目录。
    """
    manager = LLMProviderManager()
    payload = {
        "frogbot": {
            "id": "frogbot",
            "name": "FrogBot",
            "npm": "@ai-sdk/openai-compatible",
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
    缓存载荷不可用时读取随包附带的模型目录。
    """
    manager = LLMProviderManager()
    payload = {
        "frogbot": {
            "id": "frogbot",
            "name": "FrogBot",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://app.frogbot.ai/api/v1",
            "models": {},
        }
    }

    missing_cache_path = Path(f"/tmp/llm-provider-cache-missing-{id(manager)}.json")

    with (
        patch.object(manager, "_models_dev_cache_path", missing_cache_path),
        patch.object(manager, "_load_bundled_models_dev_payload", return_value=payload),
    ):
        provider = manager.get_provider("frogbot")

    assert provider.id == "frogbot"
    assert provider.default_base_url == "https://app.frogbot.ai/api/v1"


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
