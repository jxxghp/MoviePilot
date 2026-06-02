from __future__ import annotations

import importlib.util
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    module_name = f"moviepilot_local_setup_llm_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class LocalSetupLlmProviderPromptTests(unittest.TestCase):
    def test_collect_agent_config_prefers_loaded_provider_directory(self):
        module = load_local_setup_module()

        provider_definitions = [
            {
                "id": "frogbot",
                "name": "FrogBot",
                "default_base_url": "https://app.frogbot.ai/api/v1",
                "api_key_label": "API Key",
            }
        ]
        models = [
            {"id": "frog-1", "name": "Frog 1", "context_tokens_k": 128},
            {"id": "frog-2", "name": "Frog 2"},
        ]

        with patch.object(module, "print_step"), patch.object(
            module, "_prompt_yes_no", side_effect=[True, False, True]
        ), patch.object(
            module, "_load_llm_provider_definitions", return_value=provider_definitions
        ), patch.object(
            module, "_prompt_provider_choice", return_value="frogbot"
        ) as provider_prompt, patch.object(
            module, "_prompt_text", side_effect=["https://override.example.com/v1"]
        ), patch.object(
            module, "_prompt_secret_text", return_value="sk-frog"
        ), patch.object(
            module, "_load_llm_models", return_value=models
        ) as load_models, patch.object(
            module, "_prompt_model_choice", return_value="frog-2"
        ) as model_prompt, patch.object(
            module, "read_env_value", return_value=None
        ), patch.object(
            module, "_env_default", side_effect=lambda key, default="": default
        ), patch.object(
            module, "_env_bool", side_effect=lambda key, default: default
        ), patch.object(
            module, "_env_llm_thinking_level_default", return_value="auto"
        ), patch.object(
            module, "_prompt_choice", return_value="auto"
        ):
            config = module._collect_agent_config(runtime_python=Path("/tmp/runtime-python"))

        provider_prompt.assert_called_once()
        load_models.assert_called_once_with(
            provider="frogbot",
            api_key="sk-frog",
            base_url="https://override.example.com/v1",
            base_url_preset="",
            runtime_python=Path("/tmp/runtime-python"),
        )
        model_prompt.assert_called_once_with(models, default="")
        self.assertEqual(config["LLM_PROVIDER"], "frogbot")
        self.assertEqual(config["LLM_MODEL"], "frog-2")
        self.assertEqual(config["LLM_API_KEY"], "sk-frog")
        self.assertEqual(config["LLM_BASE_URL"], "https://override.example.com/v1")

    def test_collect_agent_config_falls_back_to_common_provider_choices(self):
        module = load_local_setup_module()

        with patch.object(module, "print_step"), patch.object(
            module, "_prompt_yes_no", side_effect=[True, False, True]
        ), patch.object(
            module, "_load_llm_provider_definitions", return_value=[]
        ), patch.object(
            module, "_prompt_provider_choice", return_value="anthropic"
        ), patch.object(
            module, "_prompt_text", side_effect=["https://api.anthropic.com/v1"]
        ), patch.object(
            module, "_prompt_secret_text", return_value="sk-anthropic"
        ), patch.object(
            module, "_load_llm_models", return_value=[]
        ), patch.object(
            module, "_prompt_model_choice", return_value="claude-sonnet-4-0"
        ), patch.object(
            module, "read_env_value", return_value=None
        ), patch.object(
            module, "_env_default", side_effect=lambda key, default="": default
        ), patch.object(
            module, "_env_bool", side_effect=lambda key, default: default
        ), patch.object(
            module, "_env_llm_thinking_level_default", return_value="off"
        ), patch.object(
            module, "_prompt_choice", return_value="off"
        ):
            config = module._collect_agent_config()

        self.assertEqual(config["LLM_PROVIDER"], "anthropic")
        self.assertEqual(config["LLM_MODEL"], "claude-sonnet-4-0")
        self.assertEqual(config["LLM_BASE_URL"], "https://api.anthropic.com/v1")

    def test_prompt_model_choice_accepts_index_selection(self):
        module = load_local_setup_module()

        with patch.object(module, "_print_llm_models") as print_models, patch(
            "builtins.input", return_value="2"
        ):
            model = module._prompt_model_choice(
                [
                    {"id": "model-a", "name": "Model A"},
                    {"id": "model-b", "name": "Model B"},
                ],
                default="model-a",
            )

        print_models.assert_called_once()
        self.assertEqual(model, "model-b")

    def test_prompt_model_choice_falls_back_to_text_input_when_empty(self):
        module = load_local_setup_module()

        with patch.object(module, "_prompt_text", return_value="custom-model") as prompt_text:
            model = module._prompt_model_choice([], default="")

        prompt_text.assert_called_once_with("LLM 模型名称", default="")
        self.assertEqual(model, "custom-model")

    def test_load_llm_provider_definitions_inner_uses_direct_provider_module_loader(self):
        module = load_local_setup_module()

        class _FakeManager:
            async def list_providers_async(self, force_refresh: bool = False):
                return [{"id": "frogbot", "name": "FrogBot"}]

        class _FakeProviderModule:
            @staticmethod
            def LLMProviderManager():
                return _FakeManager()

        fake_provider_module = _FakeProviderModule()

        with patch.object(
            module,
            "_load_llm_provider_module",
            return_value=fake_provider_module,
        ) as loader:
            providers = module._load_llm_provider_definitions_inner()

        loader.assert_called_once_with()
        self.assertEqual(providers, [{"id": "frogbot", "name": "FrogBot"}])

    def test_llm_provider_choice_map_skips_oauth_only_provider(self):
        module = load_local_setup_module()

        choices = module._llm_provider_choice_map(
            [
                {"id": "chatgpt", "name": "ChatGPT", "supports_api_key": True},
                {"id": "github-copilot", "name": "GitHub Copilot", "supports_api_key": False},
            ]
        )

        self.assertEqual(choices, {"chatgpt": "ChatGPT"})

    def test_prompt_provider_choice_accepts_custom_provider_id(self):
        module = load_local_setup_module()

        with patch("builtins.input", return_value="my-provider_01"), patch("builtins.print"):
            provider = module._prompt_provider_choice(
                "选择 LLM 提供商",
                {"deepseek": "DeepSeek", "google": "Google"},
                default="deepseek",
            )

        self.assertEqual(provider, "my-provider_01")

    def test_fallback_provider_choices_include_builtin_domestic_token_providers(self):
        """本地向导兜底列表应覆盖内置国内 Token 套餐 provider。"""
        module = load_local_setup_module()

        self.assertEqual(
            module.LLM_PROVIDER_FALLBACK_CHOICES["baidu-qianfan-coding-plan"],
            "百度千帆",
        )
        self.assertEqual(module.LLM_PROVIDER_FALLBACK_CHOICES["jdcloud"], "京东云")
        self.assertEqual(
            module.LLM_PROVIDER_FALLBACK_CHOICES["kuaishou-wanqing"],
            "快手万擎",
        )
        self.assertEqual(
            module.LLM_PROVIDER_FALLBACK_CHOICES["china-unicom"],
            "中国联通",
        )
        self.assertEqual(
            module.LLM_PROVIDER_FALLBACK_CHOICES["china-mobile"],
            "中国移动",
        )
        self.assertEqual(
            module.LLM_PROVIDER_FALLBACK_CHOICES["china-telecom"],
            "中国电信",
        )

    def test_local_setup_defaults_include_builtin_domestic_token_base_urls(self):
        """本地向导默认 Base URL 应覆盖内置国内 Token 套餐 provider。"""
        module = load_local_setup_module()

        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["baidu-qianfan-coding-plan"]["base_url"],
            "https://qianfan.baidubce.com/v2",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["jdcloud"]["base_url"],
            "https://modelservice.jdcloud.com/v1",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["kuaishou-wanqing"]["base_url"],
            "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["kuaishou-wanqing"]["base_url_preset"],
            "kuaishou-wanqing-usage",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-unicom"]["base_url"],
            "https://aigw-gzgy2.cucloud.cn:8443/v1",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-unicom"]["base_url_preset"],
            "china-unicom-coding-openai",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-mobile"]["base_url"],
            "https://ecloud.10086.cn/api",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-mobile"]["base_url_preset"],
            "china-mobile-moma",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-telecom"]["base_url"],
            "https://wishub-x6.ctyun.cn/v1",
        )
        self.assertEqual(
            module.LLM_PROVIDER_DEFAULTS["china-telecom"]["base_url_preset"],
            "china-telecom-token-service",
        )

    def test_collect_agent_config_prompts_for_duplicate_base_url_presets(self):
        module = load_local_setup_module()

        provider_definitions = [
            {
                "id": "minimax",
                "name": "MiniMax",
                "default_base_url": "https://api.minimaxi.com/anthropic/v1",
                "api_key_label": "API Key",
                "base_url_presets": [
                    {
                        "id": "minimax-cn-general",
                        "label": "中国内地 / 通用",
                        "value": "https://api.minimaxi.com/anthropic/v1",
                    },
                    {
                        "id": "minimax-cn-coding",
                        "label": "中国内地 / Coding Plan",
                        "value": "https://api.minimaxi.com/anthropic/v1",
                    },
                ],
            }
        ]

        with patch.object(module, "print_step"), patch.object(
            module, "_prompt_yes_no", side_effect=[True, False, True]
        ), patch.object(
            module, "_load_llm_provider_definitions", return_value=provider_definitions
        ), patch.object(
            module, "_prompt_provider_choice", return_value="minimax"
        ), patch.object(
            module, "_prompt_text", side_effect=["https://api.minimaxi.com/anthropic/v1"]
        ), patch.object(
            module, "_prompt_secret_text", return_value="sk-minimax"
        ), patch.object(
            module, "_load_llm_models", return_value=[]
        ) as load_models, patch.object(
            module, "_prompt_model_choice", return_value="MiniMax-M1"
        ), patch.object(
            module, "read_env_value", return_value=None
        ), patch.object(
            module, "_env_default", side_effect=lambda key, default="": default
        ), patch.object(
            module, "_env_bool", side_effect=lambda key, default: default
        ), patch.object(
            module, "_env_llm_thinking_level_default", return_value="auto"
        ), patch.object(
            module, "_prompt_choice", side_effect=["auto", "minimax-cn-coding"]
        ):
            config = module._collect_agent_config()

        self.assertEqual(config["LLM_BASE_URL_PRESET"], "minimax-cn-coding")
        load_models.assert_called_once_with(
            provider="minimax",
            api_key="sk-minimax",
            base_url="https://api.minimaxi.com/anthropic/v1",
            base_url_preset="minimax-cn-coding",
            runtime_python=None,
        )
