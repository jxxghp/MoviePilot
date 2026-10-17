"""
验证 DeepSeek 工具调用历史中的思考内容兼容修补。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.llm import helper as llm_module


def _build_tool_call(name: str = "search", arguments: str = "{}"):
    """
    构造固定工具调用记录，供思考内容兼容测试使用。
    """
    return [
        {
            "id": "call_1",
            "type": "tool_call",
            "name": name,
            "args": {},
        }
    ]


class _FakeChatDeepSeek:
    """
    模拟 DeepSeek 消息转请求的最小接口，不发起网络请求。
    """

    def __init__(self, model_name: str, model_kwargs: dict | None = None):
        """
        保存模型名称和扩展参数，供请求兼容逻辑读取。
        """
        self.model_name = model_name
        self.model_kwargs = model_kwargs or {}

    def _convert_input(self, input_):
        """
        将测试消息包装成兼容 to_messages 的输入对象。
        """
        return type("_FakeInput", (), {"to_messages": lambda _self: input_})()

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """
        按消息角色构造请求载荷，并保留工具调用及其关联标识。
        """
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


# 保存未修补的方法，确保每个用例从相同的请求转换行为开始。
_ORIGINAL_GET_REQUEST_PAYLOAD = _FakeChatDeepSeek._get_request_payload


@pytest.fixture
def _restore_reasoning_patch(monkeypatch):
    """
    为当前用例安装兼容补丁，由 monkeypatch 在结束时恢复原方法和标记。
    """
    monkeypatch.setattr(_FakeChatDeepSeek, "_get_request_payload", _ORIGINAL_GET_REQUEST_PAYLOAD)
    monkeypatch.setattr(_FakeChatDeepSeek, "_moviepilot_reasoning_content_patched", False, raising=False)
    if hasattr(_FakeChatDeepSeek, "_moviepilot_reasoning_content_patched"):
        delattr(_FakeChatDeepSeek, "_moviepilot_reasoning_content_patched")
    llm_module._patch_interleaved_reasoning_request_support(
        _FakeChatDeepSeek,
        patch_marker="_moviepilot_reasoning_content_patched",
        thinking_filter=lambda model_name, extra_body: llm_module._is_deepseek_thinking_enabled(model_name, extra_body),
        normalize_deepseek_messages=True,
        inject_missing_as_empty=True,
    )
    yield


@pytest.mark.usefixtures("_restore_reasoning_patch")
def test_injects_reasoning_content_for_assistant_tool_calls() -> None:
    """
    助手发起工具调用时将历史思考内容补回请求。
    """
    llm = _FakeChatDeepSeek("deepseek-v4-pro")
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


@pytest.mark.usefixtures("_restore_reasoning_patch")
def test_falls_back_to_empty_reasoning_content_when_missing() -> None:
    """
    缺少历史思考内容时补入空字符串以满足协议。
    """
    llm = _FakeChatDeepSeek("deepseek-v4-flash")
    messages = [
        HumanMessage(content="天气如何？"),
        AIMessage(content="", tool_calls=_build_tool_call()),
        ToolMessage(content="晴天", tool_call_id="call_1"),
    ]

    payload = llm._get_request_payload(messages)

    assert "reasoning_content" in payload["messages"][1]
    assert payload["messages"][1]["reasoning_content"] == ""


@pytest.mark.usefixtures("_restore_reasoning_patch")
def test_skips_injection_when_thinking_is_disabled() -> None:
    """
    关闭思考模式时不向请求补入思考字段。
    """
    llm = _FakeChatDeepSeek(
        "deepseek-v4-pro",
        model_kwargs={"extra_body": {"thinking": {"type": "disabled"}}},
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

    assert "reasoning_content" not in payload["messages"][1]
