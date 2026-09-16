"""工具图像在实际 SDK 请求中的传递、状态隔离与有界能力回退。"""

import asyncio
import base64
import json
import random
import socket
from io import BytesIO
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from PIL import Image
from pydantic import Field

from app.agent.llm.helper import LLMHelper
from app.agent.middleware.subagents import create_subagent_middlewares
from app.agent.middleware.summarization import (
    ContextPreservingSummarizationMiddleware,
    FinalRequestCompactionMiddleware,
)
from app.agent.middleware.vision import VISION_REJECTED_MODEL, VISION_UNAVAILABLE, VisionMiddleware
from app.agent.tools.impl.browse_webpage import BrowseWebpageTool
from app.agent.tools.result import TOOL_OBSERVATION_MARKER
from app.runtime.config import settings


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """SDK 序列化和脚本图测试不能意外连接真实模型或浏览器。"""
    def reject(*_args, **_kwargs):
        """发生真实解析说明测试越过了本地边界。"""
        raise AssertionError("视觉合同测试禁止外部网络")

    monkeypatch.setattr(socket, "getaddrinfo", reject)


@pytest.fixture(scope="module")
def image_url():
    """使用固定随机种子的真实 JPEG，超过普通工具文字截断上限。"""
    stream = BytesIO()
    Image.frombytes("RGB", (384, 256), random.Random(42).randbytes(384 * 256 * 3)).save(stream, format="JPEG", quality=90)
    encoded = base64.b64encode(stream.getvalue()).decode("ascii")
    assert len(encoded) > 65536
    return f"data:image/jpeg;base64,{encoded}"


def _messages(image_url):
    """一批并行工具调用中只有首个结果带图，不能在第二个回复之前插入观察。"""
    return [
        HumanMessage(content="检查页面", id="real-user"),
        AIMessage(content="", tool_calls=[
            {"id": "capture", "name": "browse_webpage", "args": {"action": "screenshot"}},
            {"id": "read", "name": "browse_webpage", "args": {"action": "get_content"}},
        ]),
        ToolMessage(content=[{"type": "text", "text": '{"url":"https://page.invalid","title":"page"}'},
                             {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}}],
                    name="browse_webpage", tool_call_id="capture"),
        ToolMessage(content="page text", name="browse_webpage", tool_call_id="read"),
    ]


def _request(messages, model=None, state=None):
    """建立真实 middleware ModelRequest，不需要启动外部模型。"""
    model = model or FakeMessagesListChatModel(responses=[AIMessage(content="reply")])
    return ModelRequest(model=model, messages=messages, state=state or {}, tools=[])


def test_projection_keeps_complete_tool_batch_and_original_graph(image_url):
    """深副本用于出站，原始调用配对、用户消息和图像均不被修改。"""
    request = _request(_messages(image_url))
    original = messages_to_dict(request.messages)
    middleware = VisionMiddleware(supports_images=lambda _: True)
    projected = middleware.project_request(request)
    assert [message.type for message in projected.messages] == ["human", "ai", "tool", "tool", "human"]
    assert [message.tool_call_id for message in projected.messages if isinstance(message, ToolMessage)] == ["capture", "read"]
    observation = projected.messages[-1]
    assert observation.additional_kwargs[TOOL_OBSERVATION_MARKER] is True
    assert "capture" in observation.content[0]["text"]
    assert "不改变真实用户要求" in observation.content[0]["text"]
    assert observation.content[1]["image_url"] == {"url": image_url, "detail": "low"}
    projected.messages[2].content[0]["text"] = "changed copy"
    observation.content[1]["image_url"]["url"] = "changed image"
    assert messages_to_dict(request.messages) == original
    assert request.state == {}
    assert messages_to_dict(middleware.project_request(request).messages) == messages_to_dict(middleware.project_request(request).messages)


@pytest.mark.parametrize("responses", [False, True, None])
def test_actual_openai_sdk_payload_contains_complete_observation_image(image_url, responses):
    """Chat、Responses 和 SDK auto 均收到图像；不依赖私有协议猜测或仅比较内部字典。"""
    model = ChatOpenAI(model="gpt-4o", api_key="test-key", base_url="https://model.invalid/v1", use_responses_api=responses)
    request = _request(_messages(image_url), model=model)
    projected = VisionMiddleware(supports_images=lambda _: True).project_request(request)
    try:
        payload = model._get_request_payload(projected.messages)
        if "input" in payload:
            tool_outputs = [entry for entry in payload["input"] if entry.get("type") == "function_call_output"]
            assert [entry["call_id"] for entry in tool_outputs] == ["capture", "read"]
            assert image_url not in json.dumps(tool_outputs)
            observed = [block for entry in payload["input"] if entry.get("role") == "user"
                        for block in entry["content"] if isinstance(block, dict) and block.get("type") == "input_image"]
            assert observed[0]["image_url"] == image_url
        else:
            assert [entry["role"] for entry in payload["messages"]] == ["user", "assistant", "tool", "tool", "user"]
            assert image_url not in json.dumps(payload["messages"][2:4])
            assert payload["messages"][-1]["content"][1]["image_url"]["url"] == image_url
    finally:
        model.root_client.close()
        asyncio.run(model.root_async_client.close())


def test_text_only_and_unknown_image_shapes_are_explicit(image_url):
    """能力关闭或未接入的图像形状都不能被描述成模型已看见。"""
    request = _request(_messages(image_url))
    projected = VisionMiddleware(supports_images=lambda _: False).project_request(request)
    assert len(projected.messages) == len(request.messages)
    assert image_url not in json.dumps(messages_to_dict(projected.messages))
    assert VISION_UNAVAILABLE in str(projected.messages[2].content)
    request.messages[2].content = [{"type": "image", "source": "unsupported-shape"}]
    unknown = VisionMiddleware(supports_images=lambda _: True).project_request(request)
    assert len(unknown.messages) == len(request.messages)
    assert VISION_UNAVAILABLE in str(unknown.messages[2].content)


def test_incomplete_tool_batch_cannot_gain_a_synthetic_user_message(image_url):
    """工具协议不完整时拒绝投影，不能编造缺失工具的成功回执。"""
    request = _request(_messages(image_url)[:-1])
    with pytest.raises(ValueError, match="回复尚未完整"):
        VisionMiddleware(supports_images=lambda _: True).project_request(request)


@pytest.mark.parametrize("source", ["data:image/jpeg;base64,not-base64*", "data:image/jpeg;base64,", "https://unknown.invalid/image.jpg"])
def test_unvalidated_tool_image_payload_does_not_gain_visual_delivery(image_url, source):
    """未知外部地址或坏编码不能仅凭 image_url 字段就进入模型视觉请求。"""
    request = _request(_messages(image_url))
    request.messages[2].content[1]["image_url"]["url"] = source
    projected = VisionMiddleware(supports_images=lambda _: True).project_request(request)
    assert len(projected.messages) == len(request.messages)
    assert VISION_UNAVAILABLE in str(projected.messages[2].content)


@pytest.mark.asyncio
async def test_unsupported_image_retries_only_model_and_records_private_turn_state(image_url):
    """已确认能力拒绝只重试模型，能力标志与原始用户或图片状态分开。"""
    request = _request(_messages(image_url))
    original = messages_to_dict(request.messages)
    calls = []
    middleware = VisionMiddleware(supports_images=lambda _: True)

    async def handler(current):
        """首次模拟服务器明确拒绝图片，第二次接受文字继续任务。"""
        calls.append(current)
        if len(calls) == 1:
            raise ValueError("This model does not support image input")
        return ModelResponse(result=[AIMessage(content="使用文字快照继续")])

    result = await middleware.awrap_model_call(request, handler)
    assert isinstance(result, ExtendedModelResponse)
    assert len(calls) == 2
    assert image_url in str(calls[0].messages[-1].content)
    assert image_url not in json.dumps(messages_to_dict(calls[1].messages))
    assert set(result.command.update) == {VISION_REJECTED_MODEL}
    assert messages_to_dict(request.messages) == original
    assert request.state == {}
    later = _request(request.messages, model=request.model, state=result.command.update)
    assert image_url not in str(middleware.project_request(later).messages)
    other = _request(request.messages)
    assert image_url in str(middleware.project_request(other).messages)
    assert middleware.before_agent(result.command.update, None) == {VISION_REJECTED_MODEL: ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("invalid request"), asyncio.CancelledError()])
async def test_other_errors_and_cancellation_do_not_retry_or_mutate(image_url, error):
    """普通错误与取消必须保留原行为，不扩大为重试整个图。"""
    request = _request(_messages(image_url))
    original = messages_to_dict(request.messages)
    calls = []

    async def handler(current):
        """记录一次模型边界失败，不执行任何工具。"""
        calls.append(current)
        raise error

    with pytest.raises(type(error)):
        await VisionMiddleware(supports_images=lambda _: True).awrap_model_call(request, handler)
    assert len(calls) == 1
    assert messages_to_dict(request.messages) == original


def test_model_image_support_uses_actual_model_and_live_switch(monkeypatch):
    """全局默认模型不能覆盖实际实例能力，用户开关改变立即生效。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)
    vision = SimpleNamespace(profile={"image_inputs": True})
    text = SimpleNamespace(profile={"image_inputs": False})
    assert LLMHelper.supports_model_image_input(vision)
    assert not LLMHelper.supports_model_image_input(text)
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", False)
    assert not LLMHelper.supports_model_image_input(vision)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_authentication_rate_and_server_errors_never_trigger_vision_fallback(status):
    """错误文本偶然含图片关键词也不能隐藏凭据、限流或服务器故障。"""
    error = RuntimeError("image input unsupported")
    error.status_code = status
    assert not LLMHelper.is_unsupported_image_input_error(error)


class _RecordingModel(FakeMessagesListChatModel):
    """记录模型真正收到的请求，图片拒绝不消耗后续脚本响应。"""

    requests: list = Field(default_factory=list)
    reject_images: bool = False

    def bind_tools(self, _tools, **_kwargs):
        """保留真实图工具执行，仅接受本地模型的工具绑定。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """有图片时模拟明确拒绝，其他请求继续固定响应。"""
        self.requests.append(messages_to_dict(messages))
        if self.reject_images and any(message.additional_kwargs.get(TOOL_OBSERVATION_MARKER) is True for message in messages):
            raise ValueError("model does not support image input")
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _browser_tool(image_url, monkeypatch):
    """浏览器传输边界固定，截图仍通过生产 Agent formatter 与权限路径。"""
    actions = []

    async def run(_self, action, **_kwargs):
        """不启动浏览器或网络，返回真实 JPEG 与已声明的截图协议。"""
        actions.append(action)
        if action == "screenshot":
            return json.dumps({"success": True, "execution_outcome": "succeeded", "format": "jpeg",
                               "screenshot_base64": image_url.split(",", 1)[1], "url": "https://page.invalid", "title": "page"})
        return json.dumps({"content": "页面文字"})

    monkeypatch.setattr(BrowseWebpageTool, "run", run)
    tool = BrowseWebpageTool(session_id="image-test", user_id="image-user")
    tool.set_agent_context({"is_admin": True})
    return tool, actions


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_real_graph_fallback_and_compaction_do_not_store_observation(image_url, monkeypatch, compact):
    """真实图把能力拒绝与压缩更新一同提交，临时 Human 不得进入 checkpoint。"""
    tool, actions = _browser_tool(image_url, monkeypatch)
    model = _RecordingModel(reject_images=True, responses=[
        AIMessage(content="", tool_calls=[{"id": "capture", "name": tool.name, "args": {"action": "screenshot"}}]),
        AIMessage(content="", tool_calls=[{"id": "text", "name": tool.name, "args": {"action": "get_content"}}]),
        AIMessage(content="按文字继续。"),
    ])
    middleware = [VisionMiddleware(supports_images=lambda _: True)]
    if compact:
        compaction = FinalRequestCompactionMiddleware(summarizer=ContextPreservingSummarizationMiddleware(model=model))

        async def prepare(request):
            """固定已有消息为保留分区，验证真实 compaction 提交与 Vision 更新的组合。"""
            if any(isinstance(message, ToolMessage) for message in request.messages):
                return [message.model_copy(deep=True) for message in request.messages]
            return None

        monkeypatch.setattr(compaction, "_aprepare_messages", prepare)
        middleware.insert(0, compaction)
    graph = create_agent(model=model, tools=[tool], middleware=middleware, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "vision-state"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="截图并核对")]}, config)
    assert actions == ["screenshot", "get_content"]
    assert len(model.requests) == 4
    assert sum(image_url in str(request) for request in model.requests) == 1
    assert result[VISION_REJECTED_MODEL]
    assert [message.type for message in result["messages"]].count("human") == 1
    assert not any(message.additional_kwargs.get(TOOL_OBSERVATION_MARKER) for message in result["messages"])
    assert image_url in str(result["messages"])
    assert graph.get_state(config).values[VISION_REJECTED_MODEL] == result[VISION_REJECTED_MODEL]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_index", [0, 1])
async def test_sync_and_background_child_providers_receive_real_images(image_url, monkeypatch, provider_index):
    """两类生产子图都装配视觉投影，权限仍由子图只读策略判断。"""
    monkeypatch.setattr(settings, "LLM_SUPPORT_IMAGE_INPUT", True)
    tool, actions = _browser_tool(image_url, monkeypatch)
    model = _RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "capture", "name": tool.name, "args": {"action": "screenshot"}}]),
        AIMessage(content="子图已收到截图。"),
    ])
    middlewares, _ = create_subagent_middlewares(model=model, tools=[tool], stream_handler=None)
    _name, graph = middlewares[provider_index]._provider.get_agent("general-purpose")
    result = await graph.ainvoke({"messages": [HumanMessage(content="观察当前页面")]})
    assert actions == ["screenshot"]
    assert image_url in str(model.requests[-1])
    assert not any(message.additional_kwargs.get(TOOL_OBSERVATION_MARKER) for message in result["messages"])
    assert any("VisionMiddleware" in node for node in graph.get_graph().nodes)
