import pytest
from langchain_core.messages import AIMessage

from app.agent import MoviePilotAgent
from app.agent.llm.helper import LLMHelper
from app.agent.llm.provider import LLMProviderManager
from app.agent.middleware.usage import UsageMiddleware
from app.chain.message import MessageChain


def test_usage_extracts_normalized_cache_details() -> None:
    """标准 usage_metadata 应解析缓存读取、写入和未命中 tokens。"""
    usage = UsageMiddleware._extract_usage(
        AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 100,
                "total_tokens": 1300,
                "input_token_details": {
                    "cache_read": 700,
                    "cache_creation": 0,
                    "ephemeral_5m_input_tokens": 300,
                },
            },
        )
    )

    assert usage["cache_usage_available"]
    assert usage["cache_read_input_tokens"] == 700
    assert usage["cache_write_input_tokens"] == 300
    assert usage["uncached_input_tokens"] == 200
    assert usage["cache_hit_ratio"] == pytest.approx(700 / 1200)


def test_usage_extracts_deepseek_cache_hit_and_miss_tokens() -> None:
    """DeepSeek 原始 usage 应保留其显式缓存命中与未命中字段。"""
    usage = UsageMiddleware._extract_usage(
        AIMessage(
            content="ok",
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                }
            },
        )
    )

    assert usage["cache_usage_available"]
    assert usage["cache_read_input_tokens"] == 800
    assert usage["cache_write_input_tokens"] == 0
    assert usage["uncached_input_tokens"] == 200
    assert usage["cache_hit_ratio"] == pytest.approx(0.8)


def test_session_usage_aggregates_and_formats_cache_statistics() -> None:
    """会话状态应聚合缓存统计并在状态文本中展示。"""
    agent = MoviePilotAgent(session_id="cache-session", user_id="user-1")
    agent._record_usage(
        {
            "has_usage": True,
            "cache_usage_available": True,
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "cache_read_input_tokens": 60,
            "cache_write_input_tokens": 20,
            "uncached_input_tokens": 20,
            "cache_hit_ratio": 0.6,
        }
    )
    agent._record_usage(
        {
            "has_usage": True,
            "cache_usage_available": True,
            "input_tokens": 50,
            "output_tokens": 5,
            "total_tokens": 55,
            "cache_read_input_tokens": 20,
            "cache_write_input_tokens": 0,
            "uncached_input_tokens": 30,
            "cache_hit_ratio": 0.4,
        }
    )

    status = agent.get_session_status()
    status.update({"is_processing": False, "pending_messages": 0})
    status_text = MessageChain._format_session_status_text(status)

    assert status["total_cache_read_input_tokens"] == 80
    assert status["total_cache_write_input_tokens"] == 20
    assert status["total_uncached_input_tokens"] == 50
    assert status["total_cache_hit_ratio"] == pytest.approx(80 / 150)
    assert "当前会话累计缓存: 命中 80 / 写入 20 / 未命中 50 (53.33%)" in status_text


def test_prompt_cache_key_is_stable_and_private() -> None:
    """提示词缓存键应在同一会话内稳定且不暴露原始标识。"""
    first = MoviePilotAgent(session_id="private-session", user_id="private-user")
    second = MoviePilotAgent(session_id="private-session", user_id="private-user")
    other = MoviePilotAgent(session_id="other-session", user_id="private-user")

    cache_key = first._build_prompt_cache_key()

    assert cache_key == second._build_prompt_cache_key()
    assert cache_key != other._build_prompt_cache_key()
    assert "private-session" not in cache_key
    assert "private-user" not in cache_key


def test_openai_prompt_cache_options_only_target_official_endpoints() -> None:
    """OpenAI 专属缓存参数不得泄露到第三方兼容端点。"""
    headers, kwargs = LLMHelper._build_openai_prompt_cache_options(
        provider="openai",
        base_url="https://api.openai.com/v1",
        use_responses_api=False,
        prompt_cache_key="cache-key",
        default_headers={"User-Agent": "MoviePilot"},
        model_kwargs={"extra_body": {"existing": True}},
    )
    _, compatible_kwargs = LLMHelper._build_openai_prompt_cache_options(
        provider="openai",
        base_url="https://api.openai.com.example/v1",
        use_responses_api=False,
        prompt_cache_key="cache-key",
        default_headers=None,
        model_kwargs={},
    )

    assert headers == {"User-Agent": "MoviePilot"}
    assert kwargs["extra_body"] == {
        "existing": True,
        "prompt_cache_key": "cache-key",
    }
    assert compatible_kwargs == {}


def test_xai_chat_completions_uses_stable_conversation_header() -> None:
    """xAI Chat Completions 应使用官方会话缓存路由请求头。"""
    headers, kwargs = LLMHelper._build_openai_prompt_cache_options(
        provider="xai",
        base_url="https://api.x.ai/v1",
        use_responses_api=None,
        prompt_cache_key="cache-key",
        default_headers=None,
        model_kwargs={},
    )

    assert headers == {"x-grok-conv-id": "cache-key"}
    assert kwargs == {}


def test_prompt_cache_adapter_preserves_control_after_tool_binding() -> None:
    """Provider 缓存参数应在 Agent 最终绑定工具时仍然存在。"""

    class FakeModel:
        """模拟通过 bind_tools 再调用 bind 的 LangChain 模型。"""

        def bind(self, **kwargs):
            """返回最终绑定参数。"""
            return kwargs

        def bind_tools(self, tools, **kwargs):
            """模拟模型的工具绑定流程。"""
            return self.bind(tools=tools, **kwargs)

    cached_model_cls = LLMHelper._with_prompt_cache_control(
        FakeModel,
        {"type": "default"},
    )

    result = cached_model_cls().bind_tools([{"name": "tool"}])

    assert result["tools"] == [{"name": "tool"}]
    assert result["cache_control"] == {"type": "default"}


def test_anthropic_cache_control_only_targets_official_endpoint() -> None:
    """Anthropic 原生缓存控制不得发送到第三方兼容端点。"""
    official = LLMHelper._use_anthropic_prompt_cache(
        provider="anthropic",
        runtime={
            "runtime": "anthropic_compatible",
            "base_url": "https://api.anthropic.com/v1",
        },
        prompt_cache_key="cache-key",
    )
    compatible = LLMHelper._use_anthropic_prompt_cache(
        provider="minimax",
        runtime={
            "runtime": "anthropic_compatible",
            "base_url": "https://api.minimax.io/anthropic/v1",
        },
        prompt_cache_key="cache-key",
    )

    assert official
    assert not compatible


def test_provider_metadata_declares_prompt_cache_without_model_allowlist() -> None:
    """Provider 应通过模型能力元数据判断缓存支持，不依赖模型 ID 白名单。"""
    assert LLMProviderManager._metadata_supports_prompt_cache(
        {"cost": {"input": 1, "cache_read": 0.1}}
    )
    assert LLMProviderManager._metadata_supports_prompt_cache(
        {"capabilities": {"prompt_cache": True}}
    )
    assert not LLMProviderManager._metadata_supports_prompt_cache(
        {"cost": {"input": 1, "output": 2}}
    )


def test_bedrock_model_metadata_candidates_remove_region_prefix() -> None:
    """Bedrock 跨区域模型应自动回落到基础模型的能力元数据。"""
    candidates = LLMProviderManager._models_dev_model_candidates(
        "amazon-bedrock",
        "us.vendor.model-version",
    )

    assert candidates == ("us.vendor.model-version", "vendor.model-version")
