import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.agent.llm import provider as provider_module
from app.agent.llm.provider import (
    LLMProviderError,
    LLMProviderManager,
    PendingAuthSession,
)


class LlmProviderRegistryTest(unittest.TestCase):
    def setUp(self):
        LLMProviderManager._instances.clear()

    def tearDown(self):
        LLMProviderManager._instances.clear()

    def test_dynamic_provider_is_exposed_from_models_dev_cache(self):
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

        self.assertEqual(provider.id, "frogbot")
        self.assertEqual(provider.runtime, "openai_compatible")
        self.assertEqual(provider.default_base_url, "https://app.frogbot.ai/api/v1")
        self.assertFalse(provider.requires_base_url)
        self.assertTrue(provider.base_url_editable)
        self.assertEqual(provider.model_list_strategy, "models_dev_only")

    def test_dynamic_provider_override_normalizes_chat_endpoint_base_url(self):
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

        self.assertEqual(provider.default_base_url, "https://api.tbox.cn/api/llm/v1")
        self.assertEqual(provider.api_key_label, "API Token")

    def test_dynamic_provider_skips_alias_only_models_dev_ids(self):
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

        with self.assertRaises(LLMProviderError):
            manager.get_provider("moonshotai")

    def test_dynamic_provider_skips_incompatible_models_dev_provider(self):
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

        with self.assertRaises(LLMProviderError):
            manager.get_provider("azure")

    def test_dynamic_provider_without_known_base_url_requires_manual_input(self):
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

        self.assertEqual(provider.runtime, "anthropic_compatible")
        self.assertTrue(provider.requires_base_url)
        self.assertTrue(provider.base_url_editable)
        self.assertEqual(provider.model_list_strategy, "anthropic_compatible")

    def test_list_providers_async_loads_models_dev_before_serializing(self):
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
                side_effect=lambda force_refresh=False, use_proxy=None: manager.__dict__.update(
                    {"_models_dev_data": payload}
                ) or payload
            ),
        ) as fetch_mock:
            providers = asyncio.run(manager.list_providers_async())

        fetch_mock.assert_awaited_once_with(force_refresh=False, use_proxy=None)
        self.assertIn("frogbot", {item["id"] for item in providers})

    def test_list_models_uses_dynamic_provider_after_refresh(self):
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

        self.assertEqual([item["id"] for item in models], ["frog-1"])
        self.assertEqual(models[0]["source"], "models.dev")

    def test_get_models_dev_data_falls_back_to_bundled_file_when_fetch_and_cache_fail(self):
        manager = LLMProviderManager()
        payload = {
            "frogbot": {
                "id": "frogbot",
                "name": "FrogBot",
                "npm": "@ai-sdk/openai-compatible",
                "models": {},
            }
        }

        with patch.object(manager, "_fetch_models_dev", AsyncMock(side_effect=RuntimeError("offline"))), patch.object(
            manager, "_load_models_dev_from_disk", AsyncMock(return_value=None)
        ), patch.object(manager, "_load_bundled_models_dev_payload", return_value=payload):
            data = asyncio.run(manager.get_models_dev_data(force_refresh=True))

        self.assertEqual(data, payload)
        self.assertEqual(manager._models_dev_data, payload)

    def test_cached_models_dev_payload_falls_back_to_bundled_file(self):
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

        with patch.object(manager, "_models_dev_cache_path", missing_cache_path), patch.object(
            manager, "_load_bundled_models_dev_payload", return_value=payload
        ):
            provider = manager.get_provider("frogbot")

        self.assertEqual(provider.id, "frogbot")
        self.assertEqual(provider.default_base_url, "https://app.frogbot.ai/api/v1")

    def test_builtin_provider_includes_baidu_qianfan_base_url_presets(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("baidu-qianfan-coding-plan")

        self.assertEqual(provider.name, "百度千帆")
        self.assertEqual(provider.runtime, "openai_compatible")
        self.assertEqual(provider.default_base_url, "https://qianfan.baidubce.com/v2")
        self.assertEqual(
            tuple((preset.label, preset.value) for preset in provider.base_url_presets),
            (
                ("通用 API", "https://qianfan.baidubce.com/v2"),
                ("Coding Plan", "https://qianfan.baidubce.com/v2/coding"),
            ),
        )
        self.assertIsNone(provider.models_dev_provider_id)
        self.assertFalse(provider.supports_model_refresh)

    def test_builtin_provider_includes_jdcloud_base_url_presets(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("jdcloud")

        self.assertEqual(provider.name, "京东云")
        self.assertEqual(provider.runtime, "openai_compatible")
        self.assertEqual(provider.default_base_url, "https://modelservice.jdcloud.com/v1")
        self.assertEqual(
            tuple((preset.label, preset.value) for preset in provider.base_url_presets),
            (
                ("通用 API", "https://modelservice.jdcloud.com/v1"),
                ("Coding Plan", "https://modelservice.jdcloud.com/coding/openai/v1"),
            ),
        )
        self.assertIsNone(provider.models_dev_provider_id)
        self.assertFalse(provider.supports_model_refresh)

    def test_builtin_provider_includes_kuaishou_wanqing_endpoint(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("kuaishou-wanqing")

        self.assertEqual(provider.name, "快手万擎")
        self.assertEqual(provider.runtime, "openai_compatible")
        self.assertEqual(
            provider.default_base_url,
            "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
        )
        self.assertEqual(
            tuple((preset.id, preset.label, preset.value) for preset in provider.base_url_presets),
            (
                (
                    "kuaishou-wanqing-usage",
                    "按量计费",
                    "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
                ),
                (
                    "kuaishou-wanqing-coding",
                    "Coding Plan",
                    "https://wanqing.streamlakeapi.com/api/gateway/coding/v1",
                ),
            ),
        )
        self.assertEqual(provider.model_list_strategy, "manual")
        self.assertFalse(provider.supports_model_refresh)

    def test_kuaishou_wanqing_coding_preset_resolves_runtime_base_url(self):
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

        self.assertEqual(runtime["runtime"], "openai_compatible")
        self.assertEqual(
            runtime["base_url"],
            "https://wanqing.streamlakeapi.com/api/gateway/coding/v1",
        )

    def test_kuaishou_wanqing_models_are_manual_input(self):
        manager = LLMProviderManager()

        models = asyncio.run(manager.list_models(provider_id="kuaishou-wanqing"))

        self.assertEqual(models, [])

    def test_builtin_provider_includes_china_operator_token_services(self):
        """三大运营商 Token 服务应作为内置 OpenAI-compatible provider 暴露。"""
        manager = LLMProviderManager()

        unicom = manager.get_provider("china-unicom")
        mobile = manager.get_provider("china-mobile")
        telecom = manager.get_provider("china-telecom")

        self.assertEqual(unicom.name, "中国联通")
        self.assertEqual(unicom.default_base_url, "https://aigw-gzgy2.cucloud.cn:8443/v1")
        self.assertEqual(
            tuple((preset.id, preset.value, preset.runtime) for preset in unicom.base_url_presets),
            (
                (
                    "china-unicom-coding-openai",
                    "https://aigw-gzgy2.cucloud.cn:8443/v1",
                    None,
                ),
                (
                    "china-unicom-coding-anthropic",
                    "https://aigw-gzgy2.cucloud.cn:8443",
                    "anthropic_compatible",
                ),
            ),
        )
        self.assertTrue(unicom.base_url_editable)
        self.assertFalse(unicom.supports_model_refresh)
        self.assertEqual(unicom.model_list_strategy, "manual")

        self.assertEqual(mobile.name, "中国移动")
        self.assertEqual(mobile.default_base_url, "https://ecloud.10086.cn/api")
        self.assertEqual(
            tuple((preset.id, preset.value) for preset in mobile.base_url_presets),
            (
                ("china-mobile-moma", "https://ecloud.10086.cn/api"),
                (
                    "china-mobile-coding",
                    "https://zhenze-huhehaote.cmecloud.cn/api/coding/v1",
                ),
            ),
        )
        self.assertTrue(mobile.base_url_editable)
        self.assertFalse(mobile.supports_model_refresh)
        self.assertEqual(mobile.model_list_strategy, "manual")

        self.assertEqual(telecom.name, "中国电信")
        self.assertEqual(telecom.default_base_url, "https://wishub-x6.ctyun.cn/v1")
        self.assertEqual(telecom.api_key_label, "App Key")
        self.assertEqual(
            tuple(
                (preset.id, preset.value, preset.runtime, preset.model_list_strategy)
                for preset in telecom.base_url_presets
            ),
            (
                (
                    "china-telecom-token-service",
                    "https://wishub-x6.ctyun.cn/v1",
                    None,
                    None,
                ),
                (
                    "china-telecom-coding-openai",
                    "https://wishub-x6.ctyun.cn/coding/v1",
                    None,
                    "manual",
                ),
                (
                    "china-telecom-coding-anthropic",
                    "https://wishub-x6.ctyun.cn/coding/v1",
                    "anthropic_compatible",
                    "manual",
                ),
            ),
        )
        self.assertTrue(telecom.base_url_editable)

    def test_china_operator_manual_model_presets_return_empty_model_list(self):
        """未提供稳定全局模型目录的运营商套餐应回退为手动填写模型。"""
        manager = LLMProviderManager()

        unicom_models = asyncio.run(manager.list_models(provider_id="china-unicom"))
        mobile_models = asyncio.run(manager.list_models(provider_id="china-mobile"))
        telecom_coding_models = asyncio.run(
            manager.list_models(
                provider_id="china-telecom",
                base_url_preset_id="china-telecom-coding-openai",
            )
        )

        self.assertEqual(unicom_models, [])
        self.assertEqual(mobile_models, [])
        self.assertEqual(telecom_coding_models, [])

    def test_china_operator_anthropic_presets_resolve_runtime(self):
        """运营商提供 Anthropic 协议地址时应切换到 anthropic_compatible runtime。"""
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

        self.assertEqual(unicom_runtime["runtime"], "anthropic_compatible")
        self.assertEqual(unicom_runtime["base_url"], "https://aigw-gzgy2.cucloud.cn:8443")
        self.assertEqual(telecom_runtime["runtime"], "anthropic_compatible")
        self.assertEqual(telecom_runtime["base_url"], "https://wishub-x6.ctyun.cn/coding")

    def test_builtin_minimax_provider_merges_general_and_coding_presets(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("minimax")
        serialized = manager.list_providers()
        minimax_payload = next(item for item in serialized if item["id"] == "minimax")

        self.assertEqual(provider.name, "MiniMax")
        self.assertEqual(provider.runtime, "anthropic_compatible")
        self.assertEqual(
            tuple((preset.id, preset.label, preset.value) for preset in provider.base_url_presets),
            (
                ("minimax-cn-general", "中国内地 / 通用", "https://api.minimaxi.com/anthropic/v1"),
                ("minimax-global-general", "国际站 / 通用", "https://api.minimax.io/anthropic/v1"),
                ("minimax-cn-coding", "中国内地 / Coding Plan", "https://api.minimaxi.com/anthropic/v1"),
                ("minimax-global-coding", "国际站 / Coding Plan", "https://api.minimax.io/anthropic/v1"),
            ),
        )
        self.assertEqual(
            tuple(item["id"] for item in minimax_payload["base_url_presets"]),
            (
                "minimax-cn-general",
                "minimax-global-general",
                "minimax-cn-coding",
                "minimax-global-coding",
            ),
        )

    def test_minimax_coding_alias_resolves_to_minimax_provider(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("minimax-coding")

        self.assertEqual(provider.id, "minimax")

    def test_resolve_models_dev_provider_id_prefers_minimax_preset_id(self):
        manager = LLMProviderManager()
        provider = manager.get_provider("minimax")

        self.assertEqual(
            manager._resolve_provider_models_dev_provider_id(
                provider,
                base_url="https://api.minimaxi.com/anthropic/v1",
                base_url_preset_id="minimax-cn-coding",
            ),
            "minimax-cn-coding-plan",
        )

    def test_builtin_moonshot_provider_includes_kimi_for_coding_preset(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("moonshot")
        serialized = manager.list_providers()
        moonshot_payload = next(item for item in serialized if item["id"] == "moonshot")

        self.assertEqual(provider.name, "Moonshot / Kimi")
        self.assertEqual(provider.runtime, "openai_compatible")
        self.assertEqual(
            tuple((preset.id, preset.label, preset.value, preset.runtime) for preset in provider.base_url_presets),
            (
                ("moonshot-cn", "中国站", "https://api.moonshot.cn/v1", None),
                ("moonshot-global", "国际站", "https://api.moonshot.ai/v1", None),
                (
                    "moonshot-kimi-coding",
                    "Kimi for Coding",
                    "https://api.kimi.com/coding/v1",
                    "anthropic_compatible",
                ),
            ),
        )
        self.assertEqual(
            tuple(item["id"] for item in moonshot_payload["base_url_presets"]),
            ("moonshot-cn", "moonshot-global", "moonshot-kimi-coding"),
        )

    def test_kimi_coding_alias_resolves_to_moonshot_provider(self):
        manager = LLMProviderManager()

        provider = manager.get_provider("kimi-coding")

        self.assertEqual(provider.id, "moonshot")

    def test_resolve_runtime_prefers_kimi_for_coding_preset_runtime(self):
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

        self.assertEqual(runtime["provider_id"], "moonshot")
        self.assertEqual(runtime["runtime"], "anthropic_compatible")
        self.assertEqual(runtime["base_url"], "https://api.kimi.com/coding")

    def test_resolve_model_list_strategy_prefers_kimi_for_coding_preset(self):
        manager = LLMProviderManager()
        provider = manager.get_provider("moonshot")

        self.assertEqual(
            manager._resolve_provider_model_list_strategy(
                provider,
                base_url="https://api.kimi.com/coding/v1",
                base_url_preset_id="moonshot-kimi-coding",
            ),
            "anthropic_compatible",
        )

    def test_chatgpt_oauth_models_follow_models_dev_catalog(self):
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
            models = asyncio.run(
                manager._list_chatgpt_oauth_models(provider_id="chatgpt")
            )

        self.assertEqual([item["id"] for item in models], ["gpt-5.5", "o4-mini"])
        self.assertTrue(all(item["source"] == "models.dev" for item in models))

    def test_chatgpt_oauth_models_return_empty_when_catalog_missing(self):
        manager = LLMProviderManager()

        with patch.object(manager, "get_models_dev_data", AsyncMock(return_value={})):
            models = asyncio.run(
                manager._list_chatgpt_oauth_models(provider_id="chatgpt")
            )

        self.assertEqual(models, [])

    def test_expired_auth_session_cleanup_removes_state_index(self):
        """过期授权会话应同时移除 session 与 OAuth state 索引。"""
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

        self.assertNotIn("session-old", manager._pending_sessions)
        self.assertNotIn("state-old", manager._oauth_state_index)
