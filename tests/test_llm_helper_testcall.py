import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


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


sys.modules.pop("app.helper.llm", None)
_stub_module(
    "app.core.config",
    settings=SimpleNamespace(
        LLM_PROVIDER="global-provider",
        LLM_MODEL="global-model",
        LLM_API_KEY="global-key",
        LLM_BASE_URL="https://global.example.com",
        LLM_TEMPERATURE=0.1,
        LLM_MAX_CONTEXT_TOKENS=64,
        PROXY_HOST=None,
    ),
)
_stub_module("app.log", logger=_DummyLogger())

module_path = Path(__file__).resolve().parents[1] / "app" / "helper" / "llm.py"
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
        get_llm_mock = Mock(return_value=fake_model)

        with patch.object(llm_module.LLMHelper, "get_llm", get_llm_mock):
            result = asyncio.run(
                llm_module.LLMHelper.test_current_settings(
                    provider="deepseek",
                    model="deepseek-chat",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                )
            )

        get_llm_mock.assert_called_once_with(
            streaming=False,
            provider="deepseek",
            model="deepseek-chat",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
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

        with patch.object(llm_module.LLMHelper, "get_llm", return_value=fake_model):
            result = asyncio.run(
                llm_module.LLMHelper.test_current_settings(
                    provider="deepseek",
                    model="deepseek-chat",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                )
            )

        self.assertNotIn("reply_preview", result)


if __name__ == "__main__":
    unittest.main()
