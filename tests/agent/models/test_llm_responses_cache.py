"""
通过真实 LangChain、OpenAI SDK 和离线传输验证连续工具调用的会话缓存路由。
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.llm import helper
from app.agent.llm.helper import LLMHelper
from app.agent.orchestrator import MoviePilotAgent


def _response(turn: int, model: str) -> dict[str, Any]:
    """
    生成两次工具调用及最终答复，响应中不伪造缓存命中率。

    :param turn: 当前离线模型请求序号，从 0 开始
    :param model: 请求使用的模型名称
    :return: 可由 OpenAI SDK 解析的 Responses 响应
    """
    output: dict[str, Any]
    if turn < 2:
        output = {
            "type": "function_call", "id": f"fc_{turn}", "call_id": f"call_{turn}",
            "name": "check_status", "arguments": json.dumps({"target": f"step-{turn}"}),
            "status": "completed",
        }
    else:
        output = {
            "type": "message", "id": "msg_done", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "完成。", "annotations": []}],
        }
    return {
        "id": f"resp_{turn}", "object": "response", "created_at": 0,
        "status": "completed", "model": model, "output": [output],
    }


def _stream_response(response: dict[str, Any]) -> str:
    """
    将同一工具响应转换为 SDK 实际消费的 SSE 事件。

    :param response: 含完整输出条目的离线响应
    :return: 包含创建、输出和完成事件的 SSE 正文
    """
    item = response["output"][0]
    events = [
        {"type": "response.created", "response": {**response, "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
    ]
    if item["type"] == "message":
        events.append({
            "type": "response.output_text.delta", "output_index": 0, "content_index": 0,
            "item_id": item["id"], "delta": item["content"][0]["text"],
        })
    events.append({"type": "response.completed", "response": response})
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("model_name", ["gpt-6-luna", "gpt-6.1-sol"])
def test_compatible_responses_preserves_cache_key_in_tool_loop(
    monkeypatch: pytest.MonkeyPatch, streaming: bool, model_name: str,
) -> None:
    """
    真实 Agent 图的三次 SDK 请求应携带同一脱敏键、稳定工具和递增消息前缀。

    :param monkeypatch: 将 HTTP 客户端和提供商发现限制到离线边界的夹具
    :param streaming: 分别覆盖流式和非流式 Responses 请求
    :param model_name: Issue 报告使用的模型，不依赖模型名称白名单
    """
    requests: list[httpx.Request] = []
    payloads: list[dict[str, Any]] = []
    targets: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """
        捕获真实 SDK 请求并提供确定性响应，禁止访问实际模型服务。

        :param request: OpenAI SDK 完成序列化后的 HTTP 请求
        :return: 与请求流式设置对应的离线 HTTP 响应
        """
        payload = json.loads(request.content)
        response = _response(len(requests), model_name)
        requests.append(request)
        payloads.append(payload)
        if payload.get("stream"):
            return httpx.Response(200, text=_stream_response(response), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=response)

    def check_status(target: str) -> str:
        """
        记录图中真实执行的工具步骤。

        :param target: 模型指定的离线步骤名称
        :return: 该步骤的确定性回执
        """
        targets.append(target)
        return f"{target} 已完成"

    agent = MoviePilotAgent(session_id="private-session", user_id="private-user")
    cache_key = agent._build_prompt_cache_key()
    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as client:
        async def invoke() -> Any:
            """
            创建真实模型并运行两次工具调用，客户端仅属于本次测试。

            :return: Agent 图最终状态
            """
            async with httpx.AsyncClient(transport=transport) as async_client:
                monkeypatch.setattr(
                    helper, "_build_httpx_client",
                    lambda _proxy, **kwargs: async_client if kwargs.get("async_client") else client,
                )
                runtime = AsyncMock()
                runtime.resolve_runtime.return_value = {
                    "runtime": "openai_compatible", "model_id": model_name,
                    "api_key": "test-key", "base_url": "https://sub2api.example/v1",
                }
                model = await LLMHelper.get_llm(
                    provider="openai", model=model_name, user_agent="", use_proxy=False,
                    api_protocol="responses", web_search_mode="disabled", streaming=streaming,
                    prompt_cache_key=cache_key, provider_runtime=runtime,
                )
                graph = create_agent(model=model, tools=[check_status], system_prompt="执行用户任务并核对工具结果。")
                return await graph.ainvoke({"messages": [HumanMessage(content="连续检查两个步骤。")]})

        result = asyncio.run(invoke())

    assert result["messages"][-1].text == "完成。"
    assert targets == ["step-0", "step-1"]
    assert len(requests) == 3
    assert all(request.url.path == "/v1/responses" for request in requests)
    assert all(payload.get("prompt_cache_key") == cache_key for payload in payloads)
    assert all(payload["tools"] == payloads[0]["tools"] for payload in payloads)
    for previous, current in zip(payloads, payloads[1:]):
        assert current["input"][:len(previous["input"])] == previous["input"]
        assert len(current["input"]) > len(previous["input"])
    assert "private-session" not in cache_key
    assert "private-user" not in cache_key
