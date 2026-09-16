"""工具调用历史修复保留真实回执，并明确缺失结果的未知状态。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.patching import PatchToolCallsMiddleware


def _build_tool_call(tool_call_id: str = "call_1", name: str = "search") -> dict:
    """构造测试用工具调用。"""
    return {
        "id": tool_call_id,
        "type": "tool_call",
        "name": name,
        "args": {},
    }


def test_adds_missing_tool_messages_immediately_after_ai_message():
    """缺失工具响应时应立即补齐 ToolMessage。"""
    middleware = PatchToolCallsMiddleware()
    messages = [
        HumanMessage(content="查天气"),
        AIMessage(content="", tool_calls=[_build_tool_call()]),
        HumanMessage(content="不用查了"),
    ]

    result = middleware.before_agent({"messages": messages}, runtime=None)

    patched_messages = result["messages"].value
    assert patched_messages[1] is messages[1]
    assert isinstance(patched_messages[2], ToolMessage)
    assert patched_messages[2].tool_call_id == "call_1"
    assert patched_messages[2].status == "error"
    assert "outcome is unknown" in patched_messages[2].content
    assert "read-only" in patched_messages[2].content
    assert patched_messages[3] is messages[2]


def test_moves_late_tool_messages_next_to_matching_ai_message():
    """乱序工具响应应移动到对应 assistant 消息之后。"""
    middleware = PatchToolCallsMiddleware()
    tool_message = ToolMessage(content="晴天", tool_call_id="call_1")
    messages = [
        HumanMessage(content="查天气"),
        AIMessage(content="", tool_calls=[_build_tool_call()]),
        HumanMessage(content="再问一句"),
        tool_message,
    ]

    result = middleware.before_agent({"messages": messages}, runtime=None)

    patched_messages = result["messages"].value
    assert patched_messages[1] is messages[1]
    assert patched_messages[2] is tool_message
    assert patched_messages[3] is messages[2]
    assert tool_message not in patched_messages[4:]


def test_drops_orphan_tool_messages():
    """孤立工具响应不应继续进入模型请求历史。"""
    middleware = PatchToolCallsMiddleware()
    orphan_tool_message = ToolMessage(content="晴天", tool_call_id="call_orphan")
    messages = [
        HumanMessage(content="查天气"),
        orphan_tool_message,
        HumanMessage(content="继续"),
    ]

    result = middleware.before_agent({"messages": messages}, runtime=None)

    patched_messages = result["messages"].value
    assert [msg.type for msg in patched_messages] == ["human", "human"]
    assert orphan_tool_message not in patched_messages


@pytest.mark.asyncio
async def test_async_hook_normalizes_messages():
    """异步 Agent 执行入口也应修复工具调用历史。"""
    middleware = PatchToolCallsMiddleware()
    messages = [
        HumanMessage(content="查天气"),
        AIMessage(content="", tool_calls=[_build_tool_call()]),
    ]

    result = await middleware.abefore_agent({"messages": messages}, runtime=None)

    patched_messages = result["messages"].value
    assert [msg.type for msg in patched_messages] == ["human", "ai", "tool"]
