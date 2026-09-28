"""兼容层回传 Anthropic 分块格式思考内容的契约测试。

网关把思考内容放在 assistant 响应 `content[].thinking` 分块中，并要求
后续请求原样回传；LangChain 在还原请求时会整体过滤这类分块，这里固定
兼容层把历史分块放回请求的行为，避免工具调用续轮被服务端以 400 拒绝。
"""

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_openai.chat_models.base import (
    _convert_delta_to_message_chunk,
    _convert_dict_to_message,
    _convert_message_to_dict,
)

from app.agent.llm import helper as llm_module

_PATCH_MARKER = "_moviepilot_test_interleaved_reasoning_patched"
_THINKING_BLOCK = {"type": "thinking", "thinking": "先查媒体库", "signature": "sig-1"}


class _FakeGatewayInput:
    """复刻 LangChain 输入对象的 to_messages 契约。"""

    def __init__(self, messages):
        self._messages = messages

    def to_messages(self):
        return self._messages


class _FakeGatewayChatModel:
    """以真实的 langchain-openai 转换函数还原请求，复现分块被过滤的行为。"""

    def __init__(self, model_name: str = "deepseek-v4-flash"):
        self.model_name = model_name
        self.model_kwargs = {}

    def _convert_input(self, input_):
        return _FakeGatewayInput(input_)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        return {
            "messages": [_convert_message_to_dict(message) for message in input_],
        }


def _build_patched_model_cls(**patch_kwargs):
    """为每个用例构造独立补丁的模型子类，避免补丁标记跨用例残留。"""

    class _PatchedGatewayChatModel(_FakeGatewayChatModel):
        """承载本次用例补丁配置的独立模型类。"""

    llm_module._patch_interleaved_reasoning_request_support(
        _PatchedGatewayChatModel,
        patch_marker=_PATCH_MARKER,
        **patch_kwargs,
    )
    return _PatchedGatewayChatModel


def _gateway_assistant_message(content):
    """按网关的 OpenAI 兼容响应构造带工具调用的 assistant 消息。"""
    return _convert_dict_to_message(
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        }
    )


def _build_tool_call_messages(content):
    """构造工具调用续轮历史，正文分块由网关响应形状决定。"""
    return [
        HumanMessage(content="查一下媒体库"),
        _gateway_assistant_message(content),
        ToolMessage(content="共 12 部", tool_call_id="call_1"),
    ]


def test_replays_thinking_blocks_before_text():
    """非流式响应中的思考分块必须原样回到续轮请求最前。"""
    llm = _build_patched_model_cls()()

    payload = llm._get_request_payload(
        _build_tool_call_messages(
            [_THINKING_BLOCK, {"type": "text", "text": "我需要先查询一下"}]
        )
    )

    assistant_payload = payload["messages"][1]
    assert assistant_payload["content"] == [
        _THINKING_BLOCK,
        {"type": "text", "text": "我需要先查询一下"},
    ]
    assert assistant_payload["tool_calls"][0]["id"] == "call_1"


def test_replays_streamed_thinking_blocks_as_blocks():
    """流式聚合出的思考分块和裸字符串正文都要还原成分块数组。"""
    llm = _build_patched_model_cls()()
    streamed = _convert_delta_to_message_chunk(
        {"role": "assistant", "content": [_THINKING_BLOCK]},
        AIMessageChunk,
    ) + _convert_delta_to_message_chunk(
        {"role": "assistant", "content": "共 12 部"},
        AIMessageChunk,
    )
    messages = [
        HumanMessage(content="查一下媒体库"),
        streamed,
        ToolMessage(content="共 12 部", tool_call_id="call_1"),
    ]

    payload = llm._get_request_payload(messages)

    assert payload["messages"][1]["content"] == [
        _THINKING_BLOCK,
        {"type": "text", "text": "共 12 部"},
    ]


@pytest.mark.parametrize(
    "block_type",
    ["thinking", "redacted_thinking", "reasoning", "reasoning_content"],
)
def test_replays_provider_thinking_block_aliases(block_type):
    """供应商思考协议的分块别名都要按原样回传，不丢字段也不改写类型。"""
    llm = _build_patched_model_cls()()
    block = {"type": block_type, "data": "opaque-payload"}

    payload = llm._get_request_payload(_build_tool_call_messages([block]))

    assert payload["messages"][1]["content"] == [block]


def test_keeps_payload_untouched_without_thinking_blocks():
    """历史里没有思考分块时不能凭空注入分块数组。"""
    llm = _build_patched_model_cls()()

    payload = llm._get_request_payload(_build_tool_call_messages("我需要先查询一下"))

    assert payload["messages"][1]["content"] == "我需要先查询一下"
    assert "reasoning_content" not in payload["messages"][1]


def test_deepseek_normalization_keeps_thinking_blocks_and_reasoning_content():
    """DeepSeek 消息扁平化不能覆盖回传的思考分块，两种协议可同时存在。"""
    llm = _build_patched_model_cls(
        thinking_filter=llm_module._is_deepseek_thinking_enabled,
        normalize_deepseek_messages=True,
        inject_missing_as_empty=True,
    )()
    messages = _build_tool_call_messages(
        [_THINKING_BLOCK, {"type": "text", "text": "我需要先查询一下"}]
    )

    payload = llm._get_request_payload(messages)

    assistant_payload = payload["messages"][1]
    assert assistant_payload["content"] == [
        _THINKING_BLOCK,
        {"type": "text", "text": "我需要先查询一下"},
    ]
    assert assistant_payload["reasoning_content"] == ""
