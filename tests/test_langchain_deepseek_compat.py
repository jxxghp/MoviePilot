import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.llm import helper as llm_module


def _build_tool_call(name: str = "search", arguments: str = "{}"):
    return [
        {
            "id": "call_1",
            "type": "tool_call",
            "name": name,
            "args": {},
        }
    ]


class _FakeChatDeepSeek:
    def __init__(self, model_name: str, model_kwargs: dict | None = None):
        self.model_name = model_name
        self.model_kwargs = model_kwargs or {}

    def _convert_input(self, input_):
        return type("_FakeInput", (), {"to_messages": lambda _self: input_})()

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


_ORIGINAL_GET_REQUEST_PAYLOAD = _FakeChatDeepSeek._get_request_payload


class DeepSeekCompatPatchTest(unittest.TestCase):
    def setUp(self):
        _FakeChatDeepSeek._get_request_payload = _ORIGINAL_GET_REQUEST_PAYLOAD
        if hasattr(_FakeChatDeepSeek, "_moviepilot_reasoning_content_patched"):
            delattr(_FakeChatDeepSeek, "_moviepilot_reasoning_content_patched")
        # helper 的修补函数内部 `from langchain_deepseek import ChatDeepSeek`，
        # 这里临时把该名指向假类，使修补作用到 _FakeChatDeepSeek；patch 在用例结束自动还原。
        patcher = patch("langchain_deepseek.ChatDeepSeek", _FakeChatDeepSeek)
        patcher.start()
        self.addCleanup(patcher.stop)
        llm_module._patch_deepseek_reasoning_content_support()

    def test_injects_reasoning_content_for_assistant_tool_calls(self):
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

        self.assertEqual(
            payload["messages"][1]["reasoning_content"],
            "先调用天气工具",
        )

    def test_falls_back_to_empty_reasoning_content_when_missing(self):
        llm = _FakeChatDeepSeek("deepseek-v4-flash")
        messages = [
            HumanMessage(content="天气如何？"),
            AIMessage(content="", tool_calls=_build_tool_call()),
            ToolMessage(content="晴天", tool_call_id="call_1"),
        ]

        payload = llm._get_request_payload(messages)

        self.assertIn("reasoning_content", payload["messages"][1])
        self.assertEqual(payload["messages"][1]["reasoning_content"], "")

    def test_skips_injection_when_thinking_is_disabled(self):
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

        self.assertNotIn("reasoning_content", payload["messages"][1])
