"""用锁定供应商 SDK 的真实请求准备入口验证工具图像观察，完全禁止出站。"""

import base64
import json
import random
import socket
from io import BytesIO
from typing import Any

import langchain_google_genai.chat_models as google_models
import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict
from langchain_google_genai import ChatGoogleGenerativeAI
from PIL import Image
from pydantic import SecretStr

from app.agent.llm.helper import _patch_gemini_thought_signature
from app.agent.middleware.vision import VisionMiddleware
from app.agent.tools.result import TOOL_OBSERVATION_MARKER

_DUMMY_KEY = "offline-vision-provider-test-key"
_CALL_IDS = ("capture_call", "read_call")
_SIGNED_THOUGHTS = {
    call_id: base64.b64encode(f"offline-signature-{call_id}".encode()).decode()
    for call_id in _CALL_IDS
}


@pytest.fixture(autouse=True)
def deny_provider_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 只能准备本地请求，连 DNS 或直接 IP 连接都立即视为测试失败。"""
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        """防止认证发现、图像 URL 下载或遥测越过离线边界。"""
        raise AssertionError("供应商视觉协议测试禁止真实网络")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture(scope="module")
def screenshot() -> tuple[bytes, str]:
    """固定随机图片超过普通文字上限，验证编码转换没有截断或重新压缩。"""
    image = Image.frombytes("RGB", (384, 256), random.Random(81).randbytes(384 * 256 * 3))
    with image, BytesIO() as stream:
        image.save(stream, format="JPEG", quality=90)
        data = stream.getvalue()
    encoded = base64.b64encode(data).decode("ascii")
    assert len(encoded) > 65536
    return data, f"data:image/jpeg;base64,{encoded}"


@pytest.fixture
def production_google_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    """局部应用生产签名兼容层，结束后还原所有 SDK 全局修改与补丁标记。"""
    for name in ("_is_gemini_3_or_later", "_parse_chat_history"):
        monkeypatch.setattr(google_models, name, getattr(google_models, name))
    monkeypatch.setattr(
        google_models, "_thought_signature_patched", getattr(google_models, "_thought_signature_patched", False),
        raising=False,
    )
    _patch_gemini_thought_signature()


def _messages(image_url: str, *, signed: bool = False) -> list[Any]:
    """一批同名浏览器工具的两个回复必须完整保留，再加入临时图片观察。"""
    return [
        HumanMessage(content="只核对页面上实际可见的信息", id="real-user"),
        AIMessage(content="", id="tool-request", tool_calls=[
            {"id": _CALL_IDS[0], "name": "browse_webpage", "args": {"action": "screenshot"}},
            {"id": _CALL_IDS[1], "name": "browse_webpage", "args": {"action": "get_content"}},
        ], additional_kwargs={"__gemini_function_call_thought_signatures__": dict(_SIGNED_THOUGHTS)} if signed else {}),
        ToolMessage(content=[
            {"type": "text", "text": '{"marker":"capture-result","url":"https://page.invalid"}'},
            {"type": "image_url", "image_url": {"url": image_url}},
        ], name="browse_webpage", tool_call_id=_CALL_IDS[0], id="capture-result"),
        ToolMessage(content='{"marker":"read-result"}', name="browse_webpage", tool_call_id=_CALL_IDS[1], id="read-result"),
    ]


def _project(model: Any, image_url: str, *, signed: bool = False) -> tuple[ModelRequest, ModelRequest, list[dict[str, Any]]]:
    """使用真正的视觉 middleware，并保留原始图快照供 SDK 转换后再比较。"""
    messages = _messages(image_url, signed=signed)
    request = ModelRequest(model=model, messages=messages, state={"messages": messages}, tools=[])
    original = messages_to_dict(messages)
    projected = VisionMiddleware(supports_images=lambda _: True).project_request(request)
    assert [message.type for message in projected.messages] == ["human", "ai", "tool", "tool", "human"]
    assert [message.tool_call_id for message in projected.messages if isinstance(message, ToolMessage)] == list(_CALL_IDS)
    assert projected.messages[-1].additional_kwargs[TOOL_OBSERVATION_MARKER] is True
    return request, projected, original


def _assert_original_unchanged(request: ModelRequest, original: list[dict[str, Any]]) -> None:
    """原始图、状态与真实用户消息都不能被 SDK 格式化或临时观察污染。"""
    assert messages_to_dict(request.messages) == original
    assert messages_to_dict(request.state["messages"]) == original
    assert not any(message.additional_kwargs.get(TOOL_OBSERVATION_MARKER) for message in request.messages)


def test_anthropic_request_payload_keeps_tool_batch_and_complete_image(screenshot: tuple[bytes, str]) -> None:
    """ChatAnthropic._get_request_payload 将观察转成合法 base64 image source，两个 tool_result 仍在最前。"""
    data, image_url = screenshot
    # model_construct 只创建配置对象，不执行客户端认证初始化；准备函数不会使用 client。
    model = ChatAnthropic.model_construct(
        model="claude-sonnet-4-5", api_key=SecretStr(_DUMMY_KEY), max_tokens=256, anthropic_proxy=None,
    )
    assert model.anthropic_api_key.get_secret_value() == _DUMMY_KEY
    request, projected, original = _project(model, image_url)
    payload = model._get_request_payload(projected.messages)
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user"]
    tool_uses = [block for block in payload["messages"][1]["content"] if block["type"] == "tool_use"]
    assert [block["id"] for block in tool_uses] == list(_CALL_IDS)
    user_blocks = payload["messages"][-1]["content"]
    assert [block["type"] for block in user_blocks] == ["tool_result", "tool_result", "text", "image"]
    assert [block["tool_use_id"] for block in user_blocks[:2]] == list(_CALL_IDS)
    assert image_url not in json.dumps(user_blocks[:2])
    source = user_blocks[-1]["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/jpeg"
    assert base64.b64decode(source["data"], validate=True) == data
    assert _CALL_IDS[0] in user_blocks[-2]["text"]
    _assert_original_unchanged(request, original)


@pytest.mark.parametrize("model_name", ["gemini-2.5-pro", "gemini-3-pro"])
@pytest.mark.parametrize("signed", [False, True])
def test_google_request_payload_keeps_inline_bytes_pairing_and_signatures(
    screenshot: tuple[bytes, str], production_google_signatures: None, model_name: str, signed: bool,
) -> None:
    """ChatGoogleGenerativeAI._prepare_request 保留图片完整字节，并维持生产的并行工具 thought signature 合同。"""
    del production_google_signatures
    data, image_url = screenshot
    model = ChatGoogleGenerativeAI.model_construct(
        model=model_name, api_key=SecretStr(_DUMMY_KEY), vertexai=False, project=None,
    )
    assert model.google_api_key.get_secret_value() == _DUMMY_KEY
    request, projected, original = _project(model, image_url, signed=signed)
    payload = model._prepare_request(projected.messages)
    assert payload["model"] == model_name
    contents = payload["contents"]
    assert [content.role for content in contents] == ["user", "model", "user", "user"]
    calls = [part for part in contents[1].parts if part.function_call is not None]
    assert [part.function_call.name for part in calls] == ["browse_webpage", "browse_webpage"]
    assert [part.function_call.args["action"] for part in calls] == ["screenshot", "get_content"]
    responses = [part.function_response for part in contents[2].parts if part.function_response is not None]
    assert [response.name for response in responses] == ["browse_webpage", "browse_webpage"]
    assert "capture-result" in json.dumps(responses[0].response)
    assert "read-result" in json.dumps(responses[1].response)
    assert not any(part.inline_data for part in contents[2].parts)
    observed = [part for part in contents[-1].parts if part.inline_data is not None]
    assert len(observed) == 1
    assert observed[0].inline_data.mime_type == "image/jpeg"
    assert observed[0].inline_data.data == data
    assert _CALL_IDS[0] in contents[-1].parts[0].text
    for index, part in enumerate(calls):
        expected = base64.b64decode(_SIGNED_THOUGHTS[_CALL_IDS[index]]) if signed else google_models.DUMMY_THOUGHT_SIGNATURE
        assert part.thought_signature == expected
    _assert_original_unchanged(request, original)
