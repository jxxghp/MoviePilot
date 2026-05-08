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


class _FakeModel:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _prompt):
        return SimpleNamespace(content=self._content)


sys.modules.pop("app.agent.llm.helper", None)
_stub_module(
    "app.core.config",
    settings=SimpleNamespace(
        LLM_PROVIDER="global-provider",
        LLM_MODEL="global-model",
        LLM_API_KEY="global-key",
        LLM_BASE_URL="https://global.example.com",
        LLM_BASE_URL_PRESET=None,
        LLM_THINKING_LEVEL=None,
        LLM_TEMPERATURE=0.1,
        LLM_MAX_CONTEXT_TOKENS=64,
        PROXY_HOST=None,
    ),
)
_stub_module("app.log", logger=_DummyLogger())

module_path = Path(__file__).resolve().parents[1] / "app" / "agent" / "llm" / "helper.py"
spec = importlib.util.spec_from_file_location("test_llm_module", module_path)
llm_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(llm_module)


class LlmHelperTestCallTest(unittest.TestCase):
    def test_extract_text_content_ignores_non_text_blocks(self):
        content = [
            {"type": "reasoning", "text": "internal"},
            {"type": "tool_use", "name": "search"},
            {"type": "text", "text": "OK"},
        ]

        result = llm_module.LLMHelper._extract_text_content(content)

        self.assertEqual(result, "OK")

    def test_test_current_settings_uses_explicit_snapshot(self):
        fake_model = _FakeModel("OK")
        get_llm_mock = AsyncMock(return_value=fake_model)

        with patch.object(llm_module.LLMHelper, "get_llm", get_llm_mock):
            result = asyncio.run(
                llm_module.LLMHelper.test_current_settings(
                    provider="deepseek",
                    model="deepseek-chat",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                    base_url_preset="deepseek-default",
                )
            )

        get_llm_mock.assert_awaited_once_with(
            streaming=False,
            provider="deepseek",
            model="deepseek-chat",
            thinking_level=None,
            api_key="sk-test",
            base_url="https://api.deepseek.com",
            base_url_preset="deepseek-default",
        )
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertEqual(result["reply_preview"], "OK")

    def test_test_current_settings_does_not_promote_non_text_blocks(self):
        fake_model = _FakeModel(
            [
                {"type": "tool_use", "name": "lookup"},
                {"type": "reasoning", "text": "thinking"},
            ]
        )

        with patch.object(
            llm_module.LLMHelper, "get_llm", AsyncMock(return_value=fake_model)
        ):
            result = asyncio.run(
                llm_module.LLMHelper.test_current_settings(
                    provider="deepseek",
                    model="deepseek-chat",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                )
            )

        self.assertNotIn("reply_preview", result)

    def test_get_llm_uses_kimi_extra_body_to_disable_thinking(self):
        calls = []

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="kimi-k2.6",
                    api_key="sk-test",
                    base_url="https://kimi.example.com/v1",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].get("extra_body"),
            {"thinking": {"type": "disabled"}},
        )

    def test_get_llm_uses_deepseek_thinking_level_controls(self):
        calls = []
        patch_calls = []

        class _FakeChatDeepSeek:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {"langchain_deepseek": SimpleNamespace(ChatDeepSeek=_FakeChatDeepSeek)},
        ), patch.object(
            llm_module,
            "_patch_deepseek_reasoning_content_support",
            side_effect=lambda: patch_calls.append(True),
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="deepseek",
                    model="deepseek-v4-pro",
                    thinking_level="xhigh",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].get("extra_body"),
            {"thinking": {"type": "enabled"}},
        )
        self.assertEqual(patch_calls, [True])
        self.assertEqual(calls[0].get("reasoning_effort"), "max")
        self.assertEqual(calls[0].get("api_base"), "https://api.deepseek.com")

    def test_get_llm_disables_deepseek_thinking_via_thinking_level(self):
        calls = []
        patch_calls = []

        class _FakeChatDeepSeek:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {"langchain_deepseek": SimpleNamespace(ChatDeepSeek=_FakeChatDeepSeek)},
        ), patch.object(
            llm_module,
            "_patch_deepseek_reasoning_content_support",
            side_effect=lambda: patch_calls.append(True),
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    thinking_level="off",
                    api_key="sk-test",
                    base_url="https://proxy.example.com",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].get("extra_body"),
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(patch_calls, [True])
        self.assertIsNone(calls[0].get("reasoning_effort"))
        self.assertEqual(calls[0].get("api_base"), "https://proxy.example.com")

    def test_get_llm_uses_openai_reasoning_effort_none_for_off(self):
        calls = []

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="gpt-5-mini",
                    thinking_level="off",
                    api_key="sk-test",
                    base_url="https://api.openai.com/v1",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("reasoning_effort"), "none")

    def test_get_llm_maps_unified_max_to_openai_xhigh(self):
        calls = []

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="gpt-5.4",
                    thinking_level="max",
                    api_key="sk-test",
                    base_url="https://api.openai.com/v1",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("reasoning_effort"), "xhigh")

    def test_get_llm_uses_gemini_builtin_thinking_controls(self):
        calls = []

        class _FakeChatGoogleGenerativeAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {
                "langchain_google_genai": SimpleNamespace(
                    ChatGoogleGenerativeAI=_FakeChatGoogleGenerativeAI
                )
            },
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="google",
                    model="gemini-2.5-flash",
                    thinking_level="off",
                    api_key="sk-test",
                    base_url=None,
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("thinking_budget"), 0)
        self.assertFalse(calls[0].get("include_thoughts"))

    def test_get_llm_uses_gemini_3_thinking_level_controls(self):
        calls = []

        class _FakeChatGoogleGenerativeAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.dict(
            sys.modules,
            {
                "langchain_google_genai": SimpleNamespace(
                    ChatGoogleGenerativeAI=_FakeChatGoogleGenerativeAI
                )
            },
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="google",
                    model="gemini-3.1-flash",
                    thinking_level="xhigh",
                    api_key="sk-test",
                    base_url=None,
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("thinking_level"), "high")
        self.assertFalse(calls[0].get("include_thoughts"))


if __name__ == "__main__":
    unittest.main()
