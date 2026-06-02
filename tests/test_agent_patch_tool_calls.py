import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.patch_tool_calls import PatchToolCallsMiddleware


def _build_tool_call(tool_call_id: str = "call_1", name: str = "search") -> dict:
    """构造测试用工具调用。"""
    return {
        "id": tool_call_id,
        "type": "tool_call",
        "name": name,
        "args": {},
    }


class TestPatchToolCallsMiddleware(unittest.TestCase):
    """测试工具调用历史修复中间件。"""

    def test_adds_missing_tool_messages_immediately_after_ai_message(self):
        """缺失工具响应时应立即补齐 ToolMessage。"""
        middleware = PatchToolCallsMiddleware()
        messages = [
            HumanMessage(content="查天气"),
            AIMessage(content="", tool_calls=[_build_tool_call()]),
            HumanMessage(content="不用查了"),
        ]

        result = middleware.before_agent({"messages": messages}, runtime=None)

        patched_messages = result["messages"].value
        self.assertIs(patched_messages[1], messages[1])
        self.assertIsInstance(patched_messages[2], ToolMessage)
        self.assertEqual(patched_messages[2].tool_call_id, "call_1")
        self.assertIs(patched_messages[3], messages[2])

    def test_moves_late_tool_messages_next_to_matching_ai_message(self):
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
        self.assertIs(patched_messages[1], messages[1])
        self.assertIs(patched_messages[2], tool_message)
        self.assertIs(patched_messages[3], messages[2])
        self.assertNotIn(tool_message, patched_messages[4:])

    def test_drops_orphan_tool_messages(self):
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
        self.assertEqual([msg.type for msg in patched_messages], ["human", "human"])
        self.assertNotIn(orphan_tool_message, patched_messages)

    def test_async_hook_normalizes_messages(self):
        """异步 Agent 执行入口也应修复工具调用历史。"""
        middleware = PatchToolCallsMiddleware()
        messages = [
            HumanMessage(content="查天气"),
            AIMessage(content="", tool_calls=[_build_tool_call()]),
        ]

        result = asyncio.run(middleware.abefore_agent({"messages": messages}, runtime=None))

        patched_messages = result["messages"].value
        self.assertEqual([msg.type for msg in patched_messages], ["human", "ai", "tool"])
