import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


def _stub_module(name: str, **attrs):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class _DummyLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _DummySystemConfigOper:
    def get(self, _key):
        return {}

    async def async_set(self, _key, _value):
        return None


for _module_name in ("aiofiles", "jwt"):
    _stub_module(_module_name)

_stub_module(
    "app.core.config",
    settings=SimpleNamespace(
        TEMP_PATH="/tmp",
        PROXY_HOST=None,
        LLM_MAX_CONTEXT_TOKENS=64,
    ),
)
_stub_module("app.db.systemconfig_oper", SystemConfigOper=_DummySystemConfigOper)
_stub_module("app.log", logger=_DummyLogger())
_stub_module("app.schemas.types", SystemConfigKey=SimpleNamespace(AIAgentConfig="agent"))

provider_path = Path(__file__).resolve().parents[1] / "app" / "agent" / "llm" / "provider.py"
spec = importlib.util.spec_from_file_location("test_llm_provider_module", provider_path)
provider_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = provider_module
spec.loader.exec_module(provider_module)

LLMProviderError = provider_module.LLMProviderError
LLMProviderManager = provider_module.LLMProviderManager


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
            AsyncMock(side_effect=lambda force_refresh=False: manager.__dict__.update({"_models_dev_data": payload}) or payload),
        ) as fetch_mock:
            providers = asyncio.run(manager.list_providers_async())

        fetch_mock.assert_awaited_once_with(force_refresh=False)
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

        async def _load_models_dev(force_refresh: bool = False):
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


if __name__ == "__main__":
    unittest.main()
