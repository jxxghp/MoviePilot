"""
验证模型请求保留有限超时，覆盖真实 SDK 的同步和异步无响应连接。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage
from openai import APITimeoutError

from app.agent.llm import helper as llm_module
from app.agent.llm.helper import LLMHelper


@pytest.fixture
def anyio_backend():
    """
    本地 HTTP 服务和 SDK 客户端共享 asyncio 事件循环。
    """
    return "asyncio"


async def _make_model(runtime, *, base_url="https://offline.example/v1"):
    """
    注入离线供应商信息，使用锁定版本的真实模型 SDK。
    """
    provider, model = {
        "openai_compatible": ("alibaba", "qwen-test"),
        "deepseek": ("deepseek", "deepseek-chat"),
        "anthropic_compatible": ("anthropic", "claude-test"),
        "google": ("google", "gemini-2.5-flash"),
    }[runtime]
    return await LLMHelper.get_llm(
        provider=provider,
        model=model,
        api_key="offline-key",
        use_proxy=False,
        thinking_level="off",
        api_protocol="chat_completions",
        web_search_mode="disabled",
        provider_runtime=SimpleNamespace(resolve_runtime=AsyncMock(return_value={
            "provider_id": provider, "runtime": runtime,
            "model_id": model, "api_key": "offline-key", "base_url": base_url,
        })),
    )


async def _close_model(model, runtime):
    """
    在创建模型的循环内回收各 SDK 拥有的同步和异步连接。
    """
    if runtime == "anthropic_compatible":
        model._client.close()
        await model._async_client.close()
    elif runtime == "google":
        await model.client.aio.aclose()
        model.client.close()
    else:
        assert await LLMHelper.close_llm(model)


@pytest.mark.anyio
@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890"])
@pytest.mark.parametrize("runtime", [
    "openai_compatible", "deepseek", "anthropic_compatible", "google",
])
async def test_real_models_have_finite_request_timeouts(monkeypatch, runtime, proxy):
    """
    供应商请求层必须显式设置超时，不能以 None 覆盖 HTTP 客户端默认值。
    """
    monkeypatch.setattr(llm_module, "_resolve_llm_proxy", lambda _use_proxy: proxy)
    model = await _make_model(runtime)
    try:
        if runtime == "google":
            assert model.timeout == 180
        elif runtime == "anthropic_compatible":
            assert model.default_request_timeout == 180
            assert model._client.timeout == 180
            assert model._async_client.timeout == 180
        else:
            assert model.request_timeout == 180
            assert model.root_client.timeout == 180
            assert model.root_async_client.timeout == 180
    finally:
        await _close_model(model, runtime)


@pytest.mark.anyio
@pytest.mark.parametrize("runtime", ["openai_compatible", "deepseek"])
@pytest.mark.parametrize("synchronous", [True, False])
async def test_silent_upstream_times_out_and_releases_connection(monkeypatch, runtime, synchronous):
    """
    上游接受连接后不回包时，真实同步 generate 和异步调用均超时并断开连接。
    """
    monkeypatch.setattr(llm_module, "_LLM_REQUEST_TIMEOUT", 0.05)
    connections = set()
    closed = asyncio.Queue()

    async def silent_response(reader, writer):
        """
        读完请求后保持静默，直到客户端超时关闭连接。
        """
        connections.add(writer)
        try:
            await reader.read()
        finally:
            connections.discard(writer)
            writer.close()
            await writer.wait_closed()
            closed.put_nowait(True)

    server = await asyncio.start_server(silent_response, "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    model = await _make_model(runtime, base_url=base_url)
    # 本用例只验证一次真实读超时，避免 SDK 退避时间影响回归耗时。
    model.root_client.max_retries = 0
    model.root_async_client.max_retries = 0
    try:
        call = (
            asyncio.to_thread(model.generate, [[HumanMessage(content="title")]])
            if synchronous else model.ainvoke("title")
        )
        with pytest.raises(APITimeoutError):
            await asyncio.wait_for(call, timeout=2)
        assert await asyncio.wait_for(closed.get(), timeout=2)
    finally:
        for writer in tuple(connections):
            writer.close()
        server.close()
        await server.wait_closed()
        await _close_model(model, runtime)
