"""LLM 独占 HTTP 客户端必须随请求和 Agent 图确定性释放。"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.agent import orchestrator as agent_module
from app.agent.llm import helper as llm_module
from app.agent.llm.helper import LLMHelper, LLMTestError, LLMTestTimeout
from app.agent.orchestrator import MoviePilotAgent, _CompiledAgentBundle
from app.agent.tools.catalog import ToolCatalogSnapshot


@pytest.fixture
def anyio_backend():
    """模型客户端只在创建它的 asyncio 事件循环内关闭。"""
    return "asyncio"


def _model(*, failure=None):
    """提供独占同步、异步连接和可配置调用结果的模型。"""
    return SimpleNamespace(
        http_client=SimpleNamespace(close=Mock(), is_closed=False),
        http_async_client=SimpleNamespace(aclose=AsyncMock(), is_closed=False),
        ainvoke=AsyncMock(return_value=SimpleNamespace(content="OK"), side_effect=failure),
    )


def _bundle(model):
    """构造持有实际模型 owner 的已编译图快照。"""
    return _CompiledAgentBundle(
        signature=("old",), agent=object(), streaming=False,
        created_at=datetime.now(), models=(model,),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, RuntimeError("failed"), TimeoutError(), asyncio.CancelledError()])
async def test_settings_test_closes_clients_on_every_exit(monkeypatch, failure):
    """设置测试在成功、异常、超时和取消后都必须释放两个客户端。"""
    model = _model(failure=failure)
    monkeypatch.setattr(LLMHelper, "get_llm", AsyncMock(return_value=model))
    if failure is None:
        assert (await LLMHelper.test_current_settings())["reply_preview"] == "OK"
    else:
        error_type = (asyncio.CancelledError if isinstance(failure, asyncio.CancelledError)
                      else LLMTestTimeout if isinstance(failure, TimeoutError) else LLMTestError)
        with pytest.raises(error_type):
            await LLMHelper.test_current_settings()
    model.http_client.close.assert_called_once()
    model.http_async_client.aclose.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, RuntimeError("failed"), asyncio.CancelledError()])
async def test_chat_title_closes_temporary_model(monkeypatch, failure):
    """标题模型不属于缓存图，调用结束时立即关闭。"""
    model = _model(failure=failure)
    agent = MoviePilotAgent(session_id="title-clients", user_id="1")
    monkeypatch.setattr(agent, "_initialize_llm", AsyncMock(return_value=model))
    if failure is None:
        await agent._generate_chat_title("测试标题")
    else:
        with pytest.raises(type(failure)):
            await agent._generate_chat_title("测试标题")
    model.http_client.close.assert_called_once()
    model.http_async_client.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_graph_replacement_closes_only_retired_clients():
    """替换缓存图时回收旧模型，保留新图直到失效；重复清理不重复关闭。"""
    old_model, new_model = _model(), _model()
    agent = MoviePilotAgent(session_id="replace-clients", user_id="1")
    agent._compiled_agent_bundle = _bundle(old_model)
    catalog = ToolCatalogSnapshot.from_tools([], plugin_revision=0, factory_revision="test")
    await agent._cache_agent(
        signature=("new",), agent=object(), streaming=False,
        tool_catalog=catalog, subagent_catalog=catalog, mcp_config_signature="",
        models=(new_model,),
    )
    old_model.http_async_client.aclose.assert_awaited_once()
    new_model.http_async_client.aclose.assert_not_awaited()
    assert await agent._invalidate_cached_agent() is True
    assert await agent._invalidate_cached_agent() is True
    new_model.http_async_client.aclose.assert_awaited_once()
    old_model.http_async_client.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_unfinished_subagent_retains_model_until_cleanup_converges():
    """仍在使用模型的子代理未退出时不得关闭连接，重试清理后才释放。"""
    model = _model()
    agent = MoviePilotAgent(session_id="pending-clients", user_id="1")
    agent._compiled_agent_bundle = _bundle(model)
    agent._subagent_middlewares = (SimpleNamespace(close=AsyncMock(side_effect=[False, True])),)
    assert await agent._invalidate_cached_agent() is False
    model.http_async_client.aclose.assert_not_awaited()
    assert await agent._invalidate_cached_agent() is True
    model.http_async_client.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_unfinished_learning_retains_model_until_task_finishes():
    """后台复盘的模型副本共享客户端，必须等其退出才能释放原图连接。"""
    model = _model()
    agent = MoviePilotAgent(session_id="learning-clients", user_id="1")
    agent._compiled_agent_bundle = _bundle(model)
    task = asyncio.get_running_loop().create_future()
    agent._learning = SimpleNamespace(run=SimpleNamespace(task=task))
    assert await agent._invalidate_cached_agent() is False
    model.http_async_client.aclose.assert_not_awaited()
    task.set_result(None)
    assert await agent._invalidate_cached_agent() is True
    model.http_async_client.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_close_failure_retains_owner_and_still_closes_other_client():
    """一个客户端关闭失败不得阻止另一个释放，Agent 保留失败 owner 供重试。"""
    model = _model()
    model.http_async_client.aclose.side_effect = [RuntimeError("close failed"), None]
    agent = MoviePilotAgent(session_id="retry-clients", user_id="1")
    agent._compiled_agent_bundle = _bundle(model)
    assert await agent._invalidate_cached_agent() is False
    model.http_client.close.assert_called_once()
    assert await agent._invalidate_cached_agent() is True
    assert model.http_async_client.aclose.await_count == 2


@pytest.mark.anyio
async def test_real_httpx_clients_close_without_collecting_model():
    """模型继续被强引用时，真实 httpx 客户端也应确定性进入关闭态。"""
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    model = SimpleNamespace(
        http_client=httpx.Client(transport=transport, trust_env=False),
        http_async_client=httpx.AsyncClient(transport=transport, trust_env=False),
    )
    try:
        await model.http_async_client.get("https://offline.example")
        assert await LLMHelper.close_llm(model) is True
        assert model.http_client.is_closed
        assert model.http_async_client.is_closed
        assert await LLMHelper.close_llm(model) is True
    finally:
        model.http_client.close()
        await model.http_async_client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890"])
@pytest.mark.parametrize("runtime", ["openai_compatible", "deepseek"])
async def test_real_models_expose_owned_clients_for_cleanup(monkeypatch, proxy, runtime):
    """锁定版本的 SDK 在代理与直连模式下都暴露由本模型独占的两个客户端。"""
    runtime_owner = SimpleNamespace(resolve_runtime=AsyncMock(return_value={
        "provider_id": "openai", "runtime": runtime,
        "model_id": "deepseek-chat" if runtime == "deepseek" else "gpt-5-mini",
        "api_key": "offline-key", "base_url": "https://offline.example/v1",
    }))
    monkeypatch.setattr(llm_module, "_resolve_llm_proxy", lambda _use_proxy: proxy)
    model = await LLMHelper.get_llm(
        provider="deepseek" if runtime == "deepseek" else "openai",
        model=runtime_owner.resolve_runtime.return_value["model_id"],
        api_key="offline-key", base_url="https://offline.example/v1",
        api_protocol="chat_completions", thinking_level="off", web_search_mode="disabled",
        provider_runtime=runtime_owner,
    )
    assert model.http_client is not None
    assert model.http_async_client is not None
    assert await LLMHelper.close_llm(model) is True
    assert model.http_client.is_closed
    assert model.http_async_client.is_closed


@pytest.mark.anyio
async def test_proxy_fin_releases_sockets_with_models_still_referenced(monkeypatch):
    """本地代理先发送 FIN，32 轮真实 SDK 请求结束后均收到客户端 EOF，不依赖 GC。"""
    # SDK 的代理 transport 会合并系统代理；只允许本用例创建的回环代理。
    monkeypatch.setattr("httpx._client.get_environment_proxies", lambda: {})
    models = []
    connections_closed = asyncio.Queue()
    writers = []

    async def proxy_response(reader, writer):
        """回放 OpenAI 响应并半关闭连接，再观测客户端是否主动释放套接字。"""
        writers.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
            await reader.readexactly(content_length)
            body = json.dumps({
                "id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
                "model": "gpt-5-mini", "choices": [{"index": 0, "finish_reason": "stop",
                                                     "message": {"role": "assistant", "content": "OK"}}],
            }).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.write_eof()
            connections_closed.put_nowait(await reader.read())
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy_response, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(llm_module, "_resolve_llm_proxy", lambda _use_proxy: proxy_url)
    runtime_owner = SimpleNamespace(resolve_runtime=AsyncMock(return_value={
        "provider_id": "openai", "runtime": "openai_compatible", "model_id": "gpt-5-mini",
        "api_key": "offline-key", "base_url": "http://offline.invalid/v1",
    }))
    get_llm = LLMHelper.get_llm

    async def make_model(**kwargs):
        """保留真实模型强引用，排除析构或 GC 自动关闭掩盖生命周期缺陷。"""
        model = await get_llm(**kwargs)
        models.append(model)
        return model

    monkeypatch.setattr(LLMHelper, "get_llm", make_model)
    try:
        for _ in range(32):
            result = await LLMHelper.test_current_settings(
                provider="openai", model="gpt-5-mini", api_key="offline-key",
                api_protocol="chat_completions", thinking_level="off", web_search_mode="disabled",
                provider_runtime=runtime_owner,
            )
            assert result["reply_preview"] == "OK"
            assert await asyncio.wait_for(connections_closed.get(), timeout=2) == b""
            assert all(model.http_client.is_closed and model.http_async_client.is_closed for model in models)
    finally:
        for model in models:
            model.http_client.close()
            await model.http_async_client.aclose()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
            await writer.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError("second model failed"), asyncio.CancelledError()])
async def test_partial_graph_construction_closes_first_model(monkeypatch, failure):
    """第二个模型构造失败或取消时，第一个已经创建的模型必须释放。"""
    model = _model()
    agent = MoviePilotAgent(session_id="partial-models", user_id="1")
    catalog = ToolCatalogSnapshot.from_tools([], plugin_revision=0, factory_revision="test")
    monkeypatch.setattr(agent, "_resolve_llm_runtime_config", AsyncMock(return_value={}))
    monkeypatch.setattr(agent, "_initialize_local_tool_catalogs", Mock(return_value=(catalog, catalog)))
    monkeypatch.setattr(agent, "_initialize_mcp_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, "_initialize_subagent_mcp_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, "_initialize_llm", AsyncMock(side_effect=[model, failure]))
    monkeypatch.setattr(agent, "_sync_model_profile", Mock())
    monkeypatch.setattr(agent_module, "_get_plugin_tools_revision", lambda: 0)
    monkeypatch.setattr(agent_module.agent_mcp_manager, "config_signature", lambda: "")
    monkeypatch.setattr(agent_module.agent_mcp_manager, "list_enabled_tool_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_module.prompt_manager, "get_agent_prompt", Mock(return_value=""))
    monkeypatch.setattr(LLMHelper, "get_server_tools", Mock(return_value=[]))
    monkeypatch.setattr(agent_module.ServerToolRegistry, "resolve_web_search",
                        Mock(return_value=SimpleNamespace(use_local_web_search=False)))
    with pytest.raises(type(failure)):
        await agent._create_agent(streaming=True)
    model.http_client.close.assert_called_once()
    model.http_async_client.aclose.assert_awaited_once()
