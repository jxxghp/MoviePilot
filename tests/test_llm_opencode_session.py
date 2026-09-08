"""OpenCode 官方端点的会话路由请求头回归测试。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.llm import helper
from app.agent.llm.helper import LLMHelper


@pytest.mark.parametrize("base_url", [
    "https://opencode.ai/zen/v1",
    "https://opencode.ai/zen/go/v1",
])
@pytest.mark.parametrize("cache_key", ["moviepilot-agent-private-hash", None])
def test_opencode_model_sends_stable_session_on_every_request(monkeypatch, base_url, cache_key):
    """主对话与独立调用经过真实 SDK 后仍携带稳定标识，工具绑定不丢失请求头。"""
    requests = []

    def respond(request):
        """在本地捕获 SDK 请求并返回最小聊天响应，禁止真实出站。"""
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
        })

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as client:
        async def invoke():
            """使用离线运行时创建模型，并验证同步与异步请求使用同一标识。"""
            async with httpx.AsyncClient(transport=transport) as async_client:
                monkeypatch.setattr(
                    helper, "_build_httpx_client",
                    lambda _proxy, **kwargs: async_client if kwargs.get("async_client") else client,
                )
                runtime = AsyncMock()
                runtime.resolve_runtime.return_value = {
                    "runtime": "openai_compatible", "model_id": "test-model",
                    "api_key": "test-key", "base_url": base_url,
                }
                model = await LLMHelper.get_llm(
                    provider="opencode", model="test-model", user_agent="",
                    use_proxy=False, api_protocol="chat_completions", web_search_mode="disabled",
                    prompt_cache_key=cache_key, provider_runtime=runtime,
                )
                bound = model.bind_tools([{
                    "type": "function", "function": {"name": "example", "description": "测试工具",
                        "parameters": {"type": "object", "properties": {}}},
                }])
                assert bound.invoke("hello").content == "OK"
                assert (await bound.ainvoke("again")).content == "OK"

        asyncio.run(invoke())

    assert len(requests) == 2
    session = requests[0].headers["x-opencode-session"]
    assert session
    assert requests[1].headers["x-opencode-session"] == session
    if cache_key:
        assert session == cache_key
    assert requests[0].headers["user-agent"] == "MoviePilot"


@pytest.mark.parametrize("base_url", [
    "https://opencode.ai/zen/go/v1", "https://OPENCODE.AI/zen/v1",
])
def test_opencode_independent_models_have_distinct_sessions(base_url):
    """没有对话标识的独立模型互不共用会话，并保留自定义 UA 和模型参数。"""
    original = {"user-agent": "custom-client/1.0", "X-Test": "value"}
    options = {"extra_body": {"test": True}}
    headers = [LLMHelper._build_openai_prompt_cache_options(
        provider="custom", base_url=base_url, use_responses_api=True,
        prompt_cache_key=None, default_headers=original, model_kwargs=options,
    ) for _ in range(2)]
    assert headers[0][0]["x-opencode-session"] != headers[1][0]["x-opencode-session"]
    assert headers[0][0]["user-agent"] == "custom-client/1.0"
    assert "User-Agent" not in headers[0][0]
    assert headers[0][0]["X-Test"] == "value"
    assert headers[0][1] == options
    assert "x-opencode-session" not in original


@pytest.mark.parametrize("base_url", [
    "https://opencode.ai.example/zen/go/v1", "https://proxy.example/opencode.ai",
    "https://opencode.ai@proxy.example/v1", "https://[invalid", None,
])
def test_opencode_headers_do_not_leak_to_other_hosts(base_url):
    """供应商名称与路径不能代替官方主机校验，兼容端点保持原有参数。"""
    headers, kwargs = LLMHelper._build_openai_prompt_cache_options(
        provider="opencode", base_url=base_url, use_responses_api=False,
        prompt_cache_key="private-cache-key", default_headers=None, model_kwargs={},
    )
    assert headers is None
    assert kwargs == {}
