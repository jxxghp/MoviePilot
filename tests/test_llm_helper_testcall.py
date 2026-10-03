import asyncio
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
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
# 在 import 期注入、退出后还原真实平台配置与日志模块，避免污染其他测试。
_config_stub = ModuleType("app.runtime.config")
_config_stub.settings = SimpleNamespace(
    LLM_PROVIDER="global-provider",
    LLM_MODEL="global-model",
    LLM_API_KEY="global-key",
    LLM_BASE_URL="https://global.example.com",
    LLM_BASE_URL_PRESET=None,
    LLM_USER_AGENT=None,
    LLM_THINKING_LEVEL=None,
    LLM_API_PROTOCOL="auto",
    LLM_TEMPERATURE=0.1,
    LLM_MAX_CONTEXT_TOKENS=64,
    LLM_USE_PROXY=True,
    PROXY_HOST=None,
)
_log_stub = ModuleType("app.runtime.log")
_log_stub.logger = _DummyLogger()

module_path = Path(__file__).resolve().parents[1] / "app" / "agent" / "llm" / "helper.py"
with stub_modules({"app.runtime.config": _config_stub, "app.runtime.log": _log_stub}):
    spec = importlib.util.spec_from_file_location("test_llm_module", module_path)
    llm_module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(llm_module)
llm_module.settings = _config_stub.settings
llm_module.get_runtime_setting = lambda key, default=None: getattr(
    _config_stub.settings, key, default
)


class _OfflineProviderManager:
    """离线 provider 解析替身，杜绝单测访问 models.dev。

    真实 ``LLMProviderManager.resolve_runtime`` 会请求 models.dev 目录、并按
    base_url 列模型，单测中走它会产生不可接受的网络 IO，且结果随外部可达性漂移。
    这里按 provider 直接给出运行时结构，provider→runtime 映射与
    Provider Runtime 的基础协议保持一致：google/gemini→google、
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
            **kwargs,
    ):
        """按 provider 返回离线运行时结构，全程不触发网络请求。

        **kwargs 吸收未来真实 resolve_runtime 可能新增的关键字参数，避免签名扩展时替身抛 TypeError。
        """
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


@contextmanager
def _use_provider_runtime(manager_cls):
    """在当前测试块内通过正式端口注入 provider 运行时替身。"""
    from app.agent.llm.gateway import register_llm_provider_runtime

    previous = register_llm_provider_runtime(lambda: manager_cls())
    try:
        yield
    finally:
        register_llm_provider_runtime(previous)


@pytest.fixture(autouse=True)
def _offline_provider_runtime():
    """默认使用离线 provider，用例结束后恢复进程原有的组合配置。"""
    from app.agent.llm.gateway import register_llm_provider_runtime

    previous = register_llm_provider_runtime(lambda: _OfflineProviderManager())
    try:
        yield
    finally:
        register_llm_provider_runtime(previous)


def test_normalize_model_profile_fills_partial_profile_from_provider_record() -> None:
    profile = llm_module.LLMHelper._normalize_model_profile(
        model_profile={
            "tool_calling": True,
            "max_output_tokens": 8192,
        },
        runtime={
            "provider_id": "google",
            "model_profile_endpoint_matched": True,
            "model_record": {
                "input_tokens": 64000,
                "output_tokens": 4096,
            },
            "model_metadata": {},
        },
    )

    assert profile["max_input_tokens"] == 64000
    assert profile["max_output_tokens"] == 4096
    assert profile["tool_calling"]


def test_normalize_model_profile_prefers_known_provider_context_limit() -> None:
    profile = llm_module.LLMHelper._normalize_model_profile(
        model_profile={"max_input_tokens": 128000, "image_inputs": True},
        runtime={
            "provider_id": "deepseek",
            "model_profile_endpoint_matched": True,
            "model_record": {
                "input_tokens": 64000,
                "context_tokens": 32768,
            },
            "model_metadata": {"limit": {"context": 64000}},
        },
    )

    assert profile["max_input_tokens"] == 32768
    assert profile["image_inputs"]


def test_normalize_model_profile_caps_unmatched_known_provider_endpoint() -> None:
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 16):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={"max_input_tokens": 128000},
            runtime={
                "provider_id": "deepseek",
                "model_profile_endpoint_matched": False,
                "model_record": {"context_tokens": 128000},
                "model_metadata": {},
            },
        )

    assert profile["max_input_tokens"] == 16000


def test_normalize_model_profile_allows_configured_limit_for_unmatched_known_endpoint() -> None:
    """未匹配的已知 provider 端点应尊重用户显式配置的上限。"""
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 512):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={"max_input_tokens": 1000000},
            runtime={
                "provider_id": "deepseek",
                "model_profile_endpoint_matched": False,
                "model_record": {"context_tokens": 1000000},
                "model_metadata": {"limit": {"input": 1000000}},
            },
        )

    assert profile["max_input_tokens"] == 512000


def test_normalize_model_profile_caps_generic_openai_endpoint() -> None:
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 32):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={"max_input_tokens": 128000},
            runtime={
                "provider_id": "openai",
                "model_record": {
                    "input_tokens": 128000,
                    "source": "models.dev-cache",
                },
                "model_metadata": {"limit": {"input": 128000}},
            },
        )

    assert profile["max_input_tokens"] == 32000


def test_normalize_model_profile_allows_configured_limit_for_generic_endpoint() -> None:
    """通用兼容端点应允许用户显式抬高默认上下文上限。"""
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 512):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={"max_input_tokens": 1000000},
            runtime={
                "provider_id": "openai",
                "model_record": {"input_tokens": 1000000},
                "model_metadata": {"limit": {"context": 1000000}},
            },
        )

    assert profile["max_input_tokens"] == 512000


def test_normalize_model_profile_keeps_smaller_generic_profile_limit() -> None:
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 256):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={
                "max_input_tokens": 64000,
                "max_output_tokens": 4096,
            },
            runtime={
                "provider_id": "openai",
                "model_record": {
                    "context_tokens": 128000,
                    "output_tokens": 16384,
                },
                "model_metadata": {"limit": {"output": 8192}},
            },
        )

    assert profile["max_input_tokens"] == 64000
    assert profile["max_output_tokens"] == 4096


def test_normalize_model_profile_uses_builtin_cap_when_config_is_invalid() -> None:
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", -1):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={"max_input_tokens": 1000000},
            runtime={
                "provider_id": "openai",
                "model_record": {},
                "model_metadata": {},
            },
        )

    assert profile["max_input_tokens"] == 256000


def test_normalize_model_profile_rejects_invalid_limits_and_uses_default() -> None:
    with patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 0):
        profile = llm_module.LLMHelper._normalize_model_profile(
            model_profile={
                "max_input_tokens": False,
                "max_output_tokens": "8192",
                "structured_output": True,
            },
            runtime={
                "provider_id": "google",
                "model_profile_endpoint_matched": True,
                "model_record": {
                    "input_tokens": True,
                    "context_tokens": -1,
                    "output_tokens": 0,
                },
                "model_metadata": {
                    "limit": {
                        "input": "64000",
                        "context": 0.0,
                        "output": -2,
                    }
                },
            },
        )

    assert profile["max_input_tokens"] == 256000
    assert "max_output_tokens" not in profile
    assert profile["structured_output"]


def test_get_llm_partial_profile_supports_fraction_summarization() -> None:
    from langchain.agents.middleware import SummarizationMiddleware

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.profile = {"tool_calling": True}

        def with_retry(self):
            """满足 LangChain 摘要模型的 Runnable 合同。"""
            return self

    with patch.dict(
        sys.modules,
        {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
    ), patch.object(llm_module.settings, "LLM_MAX_CONTEXT_TOKENS", 32):
        model = asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="openai",
                model="custom-model",
                api_key="sk-test",
                base_url="https://custom.example.com/v1",
            )
        )

    middleware = SummarizationMiddleware(
        model=model,
        trigger=("fraction", 0.85),
        token_counter=lambda _messages: 0,
    )

    assert model.profile["max_input_tokens"] == 32000
    assert model.profile["tool_calling"]
    assert middleware.model is model


def test_extract_text_content_ignores_non_text_blocks() -> None:
    content = [
        {"type": "reasoning", "text": "internal"},
        {"type": "tool_use", "name": "search"},
        {"type": "text", "text": "OK"},
    ]

    result = llm_module.LLMHelper.extract_text_content(content)

    assert result == "OK"


def test_test_current_settings_uses_explicit_snapshot() -> None:
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
        api_protocol=None,
        web_search_mode=None,
    )
    assert result["provider"] == "deepseek"
    assert result["model"] == "deepseek-chat"
    assert result["reply_preview"] == "OK"


def test_test_current_settings_does_not_promote_non_text_blocks() -> None:
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

    assert "reply_preview" not in result


def test_get_llm_uses_kimi_extra_body_to_disable_thinking() -> None:
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

    assert len(calls) == 1
    assert calls[0].get("extra_body") == {"thinking": {"type": "disabled"}}


def test_openai_compatible_patch_preserves_stream_reasoning_content() -> None:
    from langchain_core.messages import AIMessageChunk

    fake_modules, openai_base = _build_fake_openai_modules()
    with patch.dict(sys.modules, fake_modules):
        llm_module._patch_openai_interleaved_reasoning_content_support()

        chunk = openai_base._convert_delta_to_message_chunk(
            {"role": "assistant", "content": "", "reasoning_content": "先调用工具"},
            AIMessageChunk,
        )

    assert chunk.additional_kwargs.get("reasoning_content") == "先调用工具"


def test_openai_responses_patch_handles_completed_chunk_without_output() -> None:
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
        with pytest.raises(TypeError):
            openai_base._construct_lc_result_from_responses_api(
                _FakeResponse(None)
            )

        llm_module._patch_openai_responses_instructions_support()
        result = openai_base._construct_lc_result_from_responses_api(
            _FakeResponse(None),
            schema=object,
        )

    assert result.response.output == []
    assert result.kwargs.get("schema") is object


def test_openai_compatible_patch_injects_xiaomi_reasoning_content() -> None:
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

    assert payload["messages"][1]["reasoning_content"] == "先调用天气工具"


def test_openai_compatible_patch_injects_any_model_with_reasoning_content() -> None:
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

    assert payload["messages"][1]["reasoning_content"] == "先规划工具调用"


def test_openai_compatible_patch_skips_when_reasoning_content_missing() -> None:
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

    assert "reasoning_content" not in payload["messages"][1]


def test_get_llm_uses_deepseek_thinking_level_controls() -> None:
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
        "_patch_interleaved_reasoning_request_support",
        side_effect=lambda *args, **kwargs: patch_calls.append((args, kwargs)),
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

    assert len(calls) == 1
    assert calls[0].get("extra_body") == {"thinking": {"type": "enabled"}}
    assert patch_calls[0][0][0] == _FakeChatDeepSeek
    assert patch_calls[0][1]["normalize_deepseek_messages"]
    assert calls[0].get("reasoning_effort") == "max"
    assert calls[0].get("api_base") == "https://api.deepseek.com"


def test_get_llm_disables_deepseek_thinking_via_thinking_level() -> None:
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
        "_patch_interleaved_reasoning_request_support",
        side_effect=lambda *args, **kwargs: patch_calls.append((args, kwargs)),
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

    assert len(calls) == 1
    assert calls[0].get("extra_body") == {"thinking": {"type": "disabled"}}
    assert patch_calls[0][0][0] == _FakeChatDeepSeek
    assert patch_calls[0][1]["normalize_deepseek_messages"]
    assert calls[0].get("reasoning_effort") is None
    assert calls[0].get("api_base") == "https://proxy.example.com"


def test_get_llm_uses_common_responses_adapter_for_deepseek_web_search() -> None:
    """DeepSeek 服务端搜索应走通用 ChatOpenAI Responses 适配器。"""
    calls = []

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.model = kwargs["model"]
            self.profile = None

    openai_module = ModuleType("langchain_openai")
    openai_module.ChatOpenAI = _FakeChatOpenAI

    with patch.dict(sys.modules, {"langchain_openai": openai_module}), patch.object(
        llm_module,
        "_patch_openai_responses_instructions_support",
    ):
        model = asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="deepseek",
                model="deepseek-v4-flash",
                thinking_level="off",
                api_key="sk-test",
                base_url="https://api.deepseek.com",
                api_protocol="auto",
                web_search_mode="builtin",
            )
        )

    assert len(calls) == 1
    assert calls[0]["base_url"] == "https://api.deepseek.com"
    assert calls[0]["use_responses_api"]
    assert calls[0]["output_version"] == "responses/v1"
    assert llm_module.LLMHelper.get_server_tools(model) == [{"type": "web_search"}]
    assert not llm_module.LLMHelper.should_use_local_web_search(model)


def test_get_llm_rejects_unsupported_builtin_web_search() -> None:
    """强制服务端搜索不可用时应在构造模型前显式失败。"""
    with pytest.raises(ValueError, match="不支持服务端联网搜索"):
        asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="deepseek",
                model="deepseek-chat",
                thinking_level="off",
                api_key="sk-test",
                base_url="https://api.deepseek.com",
                api_protocol="auto",
                web_search_mode="builtin",
            )
        )


def test_get_llm_uses_openai_reasoning_effort_none_for_off() -> None:
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

    assert len(calls) == 1
    assert calls[0].get("reasoning_effort") == "none"


def test_get_llm_reads_latest_settings_when_runtime_args_omitted() -> None:
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

    openai_module = ModuleType("langchain_openai")
    openai_module.ChatOpenAI = _FakeChatOpenAI

    with _use_provider_runtime(_FakeProviderManager), patch.object(
        llm_module.settings, "LLM_PROVIDER", "deepseek"
    ), patch.object(
        llm_module.settings, "LLM_MODEL", "deepseek-chat"
    ), patch.object(llm_module.settings, "LLM_API_KEY", "updated-key"), patch.object(
        llm_module.settings, "LLM_BASE_URL", "https://updated.example.com/v1"
    ), patch.object(
        llm_module.settings, "LLM_BASE_URL_PRESET", "updated-preset"
    ), patch.dict(
        sys.modules,
        {
            "langchain_openai": openai_module,
        },
    ):
        asyncio.run(llm_module.LLMHelper.get_llm())

    assert len(resolve_calls) == 1
    assert (
        resolve_calls[0] == {
            "provider_id": "deepseek",
            "model": "deepseek-chat",
            "api_key": "updated-key",
            "base_url": "https://updated.example.com/v1",
            "base_url_preset_id": "updated-preset",
            "user_agent": None,
            "use_proxy": None,
        }
    )
    assert len(llm_calls) == 1
    assert llm_calls[0].get("model") == "deepseek-chat"
    assert llm_calls[0].get("api_key") == "updated-key"
    assert llm_calls[0].get("base_url") == "https://updated.example.com/v1"
    assert llm_calls[0].get("default_headers") == {"X-Test": "1"}


def test_get_llm_attaches_runtime_metadata() -> None:
    """LLM 实例应带上内部 runtime 元数据，供 Agent 中间件判断兼容分支。"""

    class _FakeProviderManager:
        async def resolve_runtime(self, **kwargs):
            return {
                "provider_id": kwargs["provider_id"],
                "runtime": "anthropic_compatible",
                "model_id": kwargs["model"],
                "api_key": kwargs["api_key"],
                "base_url": kwargs["base_url"],
                "default_headers": None,
                "use_responses_api": None,
                "model_record": None,
                "model_metadata": None,
            }

    class _FakeChatAnthropic:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.profile = None

    anthropic_module = ModuleType("langchain_anthropic")
    anthropic_module.ChatAnthropic = _FakeChatAnthropic

    with _use_provider_runtime(_FakeProviderManager), patch.dict(
        sys.modules,
        {
            "langchain_anthropic": anthropic_module,
        },
    ):
        model = asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="minimax",
                model="MiniMax-M2.7",
                api_key="sk-test",
                base_url="https://api.minimaxi.com/anthropic/v1",
            )
        )

    assert getattr(model, "_moviepilot_llm_runtime") == "anthropic_compatible"
    assert getattr(model, "_moviepilot_llm_provider_id") == "minimax"
    assert getattr(model, "_moviepilot_llm_base_url") == "https://api.minimaxi.com/anthropic/v1"


def test_get_llm_applies_proxy_only_when_enabled() -> None:
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

    assert calls[0].get("openai_proxy") == "http://proxy.example.com:7890"
    assert "http_client" not in calls[0]
    assert "http_async_client" not in calls[0]
    assert calls[1].get("openai_proxy") is None
    assert "http_client" in calls[1]
    assert "http_async_client" in calls[1]


def test_get_llm_passes_user_agent_as_openai_default_header() -> None:
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

    assert len(calls) == 1
    assert calls[0].get("default_headers") == {"User-Agent": "MoviePilot-Test/1.0"}


def test_get_llm_keeps_openai_patch_global_without_model_marker() -> None:
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

    fake_openai_modules, _ = _build_fake_openai_modules()

    with _use_provider_runtime(_FakeProviderManager), patch.dict(
        sys.modules,
        {
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
        assert (
            getattr(
                sys.modules["langchain_openai"].ChatOpenAI,
                "_moviepilot_interleaved_reasoning_patched",
                False,
            )
        )

    assert not hasattr(created, "_moviepilot_interleaved_reasoning_field")


def test_get_llm_preserves_openai_max() -> None:
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
                model="gpt-6-luna",
                thinking_level="max",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("reasoning_effort") == "max"


def test_get_llm_uses_responses_api_for_chatgpt_reasoning_models() -> None:
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
                thinking_level="xhigh",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("use_responses_api")
    assert calls[0].get("reasoning_effort") == "xhigh"


def test_get_llm_uses_gemini_builtin_thinking_controls() -> None:
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

    assert len(calls) == 1
    assert calls[0].get("thinking_budget") == 0
    assert not calls[0].get("include_thoughts")


def test_get_llm_uses_gemini_3_thinking_level_controls() -> None:
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

    assert len(calls) == 1
    assert calls[0].get("thinking_level") == "high"
    assert not calls[0].get("include_thoughts")


def test_get_llm_keeps_google_native_transport_for_compatibility_url() -> None:
    """即使配置残留 Google OpenAI 兼容地址，Google provider 仍必须使用原生 SDK。"""
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
                model="gemini-3.1-pro-preview",
                thinking_level="high",
                api_key="sk-test",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            )
        )

    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-3.1-pro-preview"
    assert "base_url" not in calls[0]


def test_get_llm_responses_protocol_forces_responses_api() -> None:
    """显式 responses 协议应让通用 OpenAI 兼容入口走 Responses API。"""
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
                model="gpt-5.6-terra",
                api_key="sk-test",
                base_url="https://example.com/v1",
                api_protocol="responses",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("use_responses_api")


def test_get_llm_chat_completions_protocol_overrides_chatgpt_auto() -> None:
    """显式 chat_completions 应覆盖 ChatGPT 官方推理模型的自动 Responses 切换。"""
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
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
                api_protocol="chat_completions",
            )
        )

    assert len(calls) == 1
    assert not calls[0].get("use_responses_api")


def test_get_llm_auto_protocol_keeps_chat_completions_for_compatible() -> None:
    """auto 协议下通用 OpenAI 兼容入口应保持默认 Chat Completions（None）。"""
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
                model="gpt-4o",
                api_key="sk-test",
                base_url="https://example.com/v1",
                api_protocol="auto",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("use_responses_api") is None


def test_get_llm_runtime_override_beats_chat_completions_protocol() -> None:
    """运行时强制 Responses（OAuth/Codex）应优先于用户 chat_completions 设置。"""
    calls = []

    class _FakeProviderManager:
        async def resolve_runtime(self, **kwargs):
            return {
                "provider_id": kwargs["provider_id"],
                "runtime": "openai_compatible",
                "model_id": kwargs["model"],
                "api_key": kwargs["api_key"],
                "base_url": kwargs["base_url"],
                "default_headers": None,
                "use_responses_api": True,
                "model_record": None,
                "model_metadata": None,
            }

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.model = kwargs["model"]
            self.profile = None

    with _use_provider_runtime(_FakeProviderManager), patch.dict(
        sys.modules,
        {
            "langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI),
        },
    ):
        asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="chatgpt",
                model="gpt-5.4",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
                api_protocol="chat_completions",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("use_responses_api")


def test_get_llm_reads_api_protocol_from_settings_when_omitted() -> None:
    """未显式传入协议时应读取 LLM_API_PROTOCOL 配置。"""
    calls = []

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.model = kwargs["model"]
            self.profile = None

    with patch.object(
        llm_module.settings, "LLM_API_PROTOCOL", "responses"
    ), patch.dict(
        sys.modules,
        {"langchain_openai": SimpleNamespace(ChatOpenAI=_FakeChatOpenAI)},
    ):
        asyncio.run(
            llm_module.LLMHelper.get_llm(
                provider="openai",
                model="gpt-5.6-terra",
                api_key="sk-test",
                base_url="https://example.com/v1",
            )
        )

    assert len(calls) == 1
    assert calls[0].get("use_responses_api")


def test_normalize_api_protocol_accepts_known_and_falls_back() -> None:
    """_normalize_api_protocol 应大小写不敏感识别已知值，未知值回退 auto。"""
    assert llm_module.LLMHelper._normalize_api_protocol("Responses") == "responses"
    assert llm_module.LLMHelper._normalize_api_protocol("CHAT_COMPLETIONS") == "chat_completions"
    assert llm_module.LLMHelper._normalize_api_protocol("auto") == "auto"
    assert llm_module.LLMHelper._normalize_api_protocol(None) == "auto"
    assert llm_module.LLMHelper._normalize_api_protocol("weird") == "auto"
