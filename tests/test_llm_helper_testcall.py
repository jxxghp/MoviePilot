import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.testing import stub_modules


class _DummyLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _FakeModel:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _prompt):
        return SimpleNamespace(content=self._content)


def _build_tool_call(name: str = "search"):
    return [
        {
            "id": "call_1",
            "type": "tool_call",
            "name": name,
            "args": {},
        }
    ]


class _FakeOpenAIInput:
    def __init__(self, messages):
        self._messages = messages

    def to_messages(self):
        return self._messages


class _FakeChatOpenAIForPatch:
    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.model_name = kwargs["model"]
        self.openai_api_base = kwargs.get("base_url")
        self.profile = None

    def _convert_input(self, input_):
        return _FakeOpenAIInput(input_)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = []
        for message in input_:
            payload_message = {
                "role": message.type,
                "content": message.content,
            }
            if message.type == "human":
                payload_message["role"] = "user"
            elif message.type == "ai":
                payload_message["role"] = "assistant"
                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    payload_message["tool_calls"] = tool_calls
            elif message.type == "tool":
                payload_message["role"] = "tool"
                payload_message["tool_call_id"] = message.tool_call_id
            messages.append(payload_message)
        return {"messages": messages}


def _build_fake_openai_modules(chat_openai_cls=_FakeChatOpenAIForPatch):
    """构造最小 langchain_openai stub，避免单测触发真实依赖链。"""
    from langchain_core.messages import AIMessageChunk

    for attr in (
            "_moviepilot_interleaved_reasoning_patched",
            "_moviepilot_responses_instructions_patched",
    ):
        if hasattr(chat_openai_cls, attr):
            delattr(chat_openai_cls, attr)

    openai_module = ModuleType("langchain_openai")
    openai_module.__path__ = []
    openai_module.ChatOpenAI = chat_openai_cls

    chat_models_module = ModuleType("langchain_openai.chat_models")
    chat_models_module.__path__ = []

    base_module = ModuleType("langchain_openai.chat_models.base")

    def _convert_dict_to_message(message_dict):
        return AIMessage(content=message_dict.get("content") or "")

    def _convert_delta_to_message_chunk(delta, default_class):
        return AIMessageChunk(content=delta.get("content") or "")

    def _construct_lc_result_from_responses_api(response, *args, **kwargs):
        """模拟旧版 langchain-openai 直接遍历 response.output 的行为。"""
        for _item in response.output:
            pass
        return SimpleNamespace(args=args, kwargs=kwargs, response=response)

    base_module._convert_dict_to_message = _convert_dict_to_message
    base_module._convert_delta_to_message_chunk = _convert_delta_to_message_chunk
    base_module._construct_lc_result_from_responses_api = (
        _construct_lc_result_from_responses_api
    )

    return {
        "langchain_openai": openai_module,
        "langchain_openai.chat_models": chat_models_module,
        "langchain_openai.chat_models.base": base_module,
    }, base_module


# 以假 settings/log 控制 helper 加载期行为；用唯一模块名加载，并以 stub_modules 上下文
# 在 import 期注入、退出后还原真实 app.core.config / app.log，避免污染其他测试。
_config_stub = ModuleType("app.core.config")
_config_stub.settings = SimpleNamespace(
    LLM_PROVIDER="global-provider",
    LLM_MODEL="global-model",
    LLM_API_KEY="global-key",
    LLM_BASE_URL="https://global.example.com",
    LLM_BASE_URL_PRESET=None,
    LLM_USER_AGENT=None,
    LLM_THINKING_LEVEL=None,
    LLM_TEMPERATURE=0.1,
    LLM_MAX_CONTEXT_TOKENS=64,
    LLM_USE_PROXY=True,
    PROXY_HOST=None,
)
_log_stub = ModuleType("app.log")
_log_stub.logger = _DummyLogger()

module_path = Path(__file__).resolve().parents[1] / "app" / "agent" / "llm" / "helper.py"
with stub_modules({"app.core.config": _config_stub, "app.log": _log_stub}):
    spec = importlib.util.spec_from_file_location("test_llm_module", module_path)
    llm_module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(llm_module)


class _OfflineProviderManager:
    """离线 provider 解析替身，杜绝单测访问 models.dev。

    真实 ``LLMProviderManager.resolve_runtime`` 会请求 models.dev 目录、并按
    base_url 列模型，单测中走它会产生不可接受的网络 IO，且结果随外部可达性漂移。
    这里按 provider 直接给出运行时结构，provider→runtime 映射与
    ``helper._build_legacy_runtime`` 保持一致：google/gemini→google、
    deepseek→deepseek、其余→openai_compatible；``use_responses_api`` 等留空，
    交由 ``get_llm`` 自身逻辑（如 ChatGPT 官方推理模型）推导，避免改变被测行为。
    """

    # provider 标识到运行时类型的映射，与 helper 内置回退逻辑保持一致
    _RUNTIME_BY_PROVIDER = {
        "google": "google",
        "gemini": "google",
        "deepseek": "deepseek",
    }

    async def resolve_runtime(
            self,
            *,
            provider_id,
            model=None,
            api_key=None,
            base_url=None,
            base_url_preset_id=None,
            user_agent=None,
            use_proxy=None,
    ):
        """按 provider 返回离线运行时结构，全程不触发网络请求。"""
        normalized = (provider_id or "").strip().lower()
        return {
            "provider_id": normalized,
            "runtime": self._RUNTIME_BY_PROVIDER.get(normalized, "openai_compatible"),
            "model_id": model,
            "api_key": api_key,
            "base_url": base_url,
            "default_headers": None,
            "use_responses_api": None,
            "model_record": None,
            "model_metadata": None,
        }


class LlmHelperTestCallTest(unittest.TestCase):
    def setUp(self):
        """为每个用例默认注入离线 provider，确保 get_llm 不会真访问 models.dev。

        需要校验特定 resolve_runtime 行为的用例，可在自身 patch.dict 中再覆盖
        ``sys.modules['app.agent.llm.provider']``；用例结束后由 addCleanup 还原。
        """
        provider_module = ModuleType("app.agent.llm.provider")
        provider_module.LLMProviderManager = _OfflineProviderManager
        patcher = patch.dict(sys.modules, {"app.agent.llm.provider": provider_module})
        patcher.start()
        self.addCleanup(patcher.stop)

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
            user_agent=None,
            use_proxy=None,
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

    def test_openai_compatible_patch_preserves_stream_reasoning_content(self):
        from langchain_core.messages import AIMessageChunk

        fake_modules, openai_base = _build_fake_openai_modules()
        with patch.dict(sys.modules, fake_modules):
            llm_module._patch_openai_interleaved_reasoning_content_support()

            chunk = openai_base._convert_delta_to_message_chunk(
                {"role": "assistant", "content": "", "reasoning_content": "先调用工具"},
                AIMessageChunk,
            )

        self.assertEqual(
            chunk.additional_kwargs.get("reasoning_content"),
            "先调用工具",
        )

    def test_openai_responses_patch_handles_completed_chunk_without_output(self):
        """校验 Responses API 流式完成事件 output 为空时不再崩溃。"""

        class _FakeResponse:
            """模拟 OpenAI Responses API 完成事件里的 Response 对象。"""

            def __init__(self, output):
                """保存 output 字段用于复现空输出场景。"""
                self.output = output

            def model_copy(self, update=None):
                """模拟 Pydantic v2 model_copy(update=...) 行为。"""
                copied = _FakeResponse(self.output)
                for key, value in (update or {}).items():
                    setattr(copied, key, value)
                return copied

        fake_modules, openai_base = _build_fake_openai_modules()
        with patch.dict(sys.modules, fake_modules):
            with self.assertRaises(TypeError):
                openai_base._construct_lc_result_from_responses_api(
                    _FakeResponse(None)
                )

            llm_module._patch_openai_responses_instructions_support()
            result = openai_base._construct_lc_result_from_responses_api(
                _FakeResponse(None),
                schema=object,
            )

        self.assertEqual(result.response.output, [])
        self.assertEqual(result.kwargs.get("schema"), object)

    def test_openai_compatible_patch_injects_xiaomi_reasoning_content(self):
        fake_modules, _ = _build_fake_openai_modules()
        with patch.dict(sys.modules, fake_modules):
            llm_module._patch_openai_interleaved_reasoning_content_support()
            llm = _FakeChatOpenAIForPatch(
                model="mimo-v2.5-pro",
                api_key="sk-test",
                base_url="https://api.xiaomimimo.com/v1",
            )
            messages = [
                HumanMessage(content="天气如何？"),
                AIMessage(
                    content="",
                    tool_calls=_build_tool_call(),
                    additional_kwargs={"reasoning_content": "先调用天气工具"},
                ),
                ToolMessage(content="晴天", tool_call_id="call_1"),
            ]

            payload = llm._get_request_payload(messages)

        self.assertEqual(
            payload["messages"][1]["reasoning_content"],
            "先调用天气工具",
        )

    def test_openai_compatible_patch_injects_any_model_with_reasoning_content(self):
        fake_modules, _ = _build_fake_openai_modules()
        with patch.dict(sys.modules, fake_modules):
            llm_module._patch_openai_interleaved_reasoning_content_support()
            llm = _FakeChatOpenAIForPatch(
                model="glm-5",
                api_key="sk-test",
                base_url="https://open.bigmodel.cn/api/paas/v4",
            )
            messages = [
                HumanMessage(content="天气如何？"),
                AIMessage(
                    content="",
                    tool_calls=_build_tool_call(),
                    additional_kwargs={"reasoning_content": "先规划工具调用"},
                ),
                ToolMessage(content="晴天", tool_call_id="call_1"),
            ]

            payload = llm._get_request_payload(messages)

        self.assertEqual(
            payload["messages"][1]["reasoning_content"],
            "先规划工具调用",
        )

    def test_openai_compatible_patch_skips_when_reasoning_content_missing(self):
        fake_modules, _ = _build_fake_openai_modules()
        with patch.dict(sys.modules, fake_modules):
            llm_module._patch_openai_interleaved_reasoning_content_support()
            llm = _FakeChatOpenAIForPatch(
                model="gpt-4o-mini",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
            )
            messages = [
                HumanMessage(content="天气如何？"),
                AIMessage(
                    content="",
                    tool_calls=_build_tool_call(),
                ),
                ToolMessage(content="晴天", tool_call_id="call_1"),
            ]

            payload = llm._get_request_payload(messages)

        self.assertNotIn("reasoning_content", payload["messages"][1])

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

    def test_get_llm_reads_latest_settings_when_runtime_args_omitted(self):
        resolve_calls = []
        llm_calls = []

        class _FakeProviderManager:
            async def resolve_runtime(self, **kwargs):
                resolve_calls.append(kwargs)
                return {
                    "provider_id": kwargs["provider_id"],
                    "runtime": "openai_compatible",
                    "model_id": kwargs["model"],
                    "api_key": kwargs["api_key"],
                    "base_url": kwargs["base_url"],
                    "default_headers": {"X-Test": "1"},
                    "use_responses_api": None,
                    "model_record": None,
                    "model_metadata": None,
                }

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                llm_calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        provider_module = ModuleType("app.agent.llm.provider")
        provider_module.LLMProviderManager = _FakeProviderManager
        openai_module = ModuleType("langchain_openai")
        openai_module.ChatOpenAI = _FakeChatOpenAI

        with patch.object(llm_module.settings, "LLM_PROVIDER", "deepseek"), patch.object(
            llm_module.settings, "LLM_MODEL", "deepseek-chat"
        ), patch.object(llm_module.settings, "LLM_API_KEY", "updated-key"), patch.object(
            llm_module.settings, "LLM_BASE_URL", "https://updated.example.com/v1"
        ), patch.object(
            llm_module.settings, "LLM_BASE_URL_PRESET", "updated-preset"
        ), patch.dict(
            sys.modules,
            {
                "app.agent.llm.provider": provider_module,
                "langchain_openai": openai_module,
            },
        ):
            asyncio.run(llm_module.LLMHelper.get_llm())

        self.assertEqual(len(resolve_calls), 1)
        self.assertEqual(
            resolve_calls[0],
            {
                "provider_id": "deepseek",
                "model": "deepseek-chat",
                "api_key": "updated-key",
                "base_url": "https://updated.example.com/v1",
                "base_url_preset_id": "updated-preset",
                "user_agent": None,
                "use_proxy": None,
            },
        )
        self.assertEqual(len(llm_calls), 1)
        self.assertEqual(llm_calls[0].get("model"), "deepseek-chat")
        self.assertEqual(llm_calls[0].get("api_key"), "updated-key")
        self.assertEqual(
            llm_calls[0].get("base_url"),
            "https://updated.example.com/v1",
        )
        self.assertEqual(llm_calls[0].get("default_headers"), {"X-Test": "1"})

    def test_get_llm_applies_proxy_only_when_enabled(self):
        """LLM 构造时应按独立开关决定是否传入系统代理。"""
        calls = []

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.model = kwargs["model"]
                self.profile = None

        with patch.object(llm_module.settings, "PROXY_HOST", "http://proxy.example.com:7890"), patch.dict(
            sys.modules,
            {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
        ):
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="gpt-5-mini",
                    api_key="sk-test",
                    base_url="https://api.example.com/v1",
                    use_proxy=True,
                )
            )
            asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="gpt-5-mini",
                    api_key="sk-test",
                    base_url="https://api.example.com/v1",
                    use_proxy=False,
                )
            )

        self.assertEqual(calls[0].get("openai_proxy"), "http://proxy.example.com:7890")
        self.assertNotIn("http_client", calls[0])
        self.assertNotIn("http_async_client", calls[0])
        self.assertIsNone(calls[1].get("openai_proxy"))
        self.assertIn("http_client", calls[1])
        self.assertIn("http_async_client", calls[1])

    def test_get_llm_passes_user_agent_as_openai_default_header(self):
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
                    api_key="sk-test",
                    base_url="https://api.example.com/v1",
                    user_agent="MoviePilot-Test/1.0",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].get("default_headers"),
            {"User-Agent": "MoviePilot-Test/1.0"},
        )

    def test_get_llm_keeps_openai_patch_global_without_model_marker(self):
        class _FakeProviderManager:
            async def resolve_runtime(self, **kwargs):
                return {
                    "provider_id": kwargs["provider_id"],
                    "runtime": "openai_compatible",
                    "model_id": kwargs["model"],
                    "api_key": kwargs["api_key"],
                    "base_url": kwargs["base_url"],
                    "default_headers": None,
                    "use_responses_api": None,
                    "model_record": None,
                    "model_metadata": {},
                }

        provider_module = ModuleType("app.agent.llm.provider")
        provider_module.LLMProviderManager = _FakeProviderManager
        fake_openai_modules, _ = _build_fake_openai_modules()

        with patch.dict(
            sys.modules,
            {
                "app.agent.llm.provider": provider_module,
                **fake_openai_modules,
            },
        ):
            created = asyncio.run(
                llm_module.LLMHelper.get_llm(
                    provider="openai",
                    model="mimo-v2.5-pro",
                    api_key="sk-test",
                    base_url="https://api.xiaomimimo.com/v1",
                )
            )
            self.assertTrue(
                getattr(
                    sys.modules["langchain_openai"].ChatOpenAI,
                    "_moviepilot_interleaved_reasoning_patched",
                    False,
                )
            )

        self.assertFalse(hasattr(created, "_moviepilot_interleaved_reasoning_field"))

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

    def test_get_llm_uses_responses_api_for_chatgpt_reasoning_models(self):
        """校验 ChatGPT 官方推理模型会切换到 Responses API。"""
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
                    provider="chatgpt",
                    model="gpt-5.4",
                    thinking_level="max",
                    api_key="sk-test",
                    base_url="https://api.openai.com/v1",
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("use_responses_api"))
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
