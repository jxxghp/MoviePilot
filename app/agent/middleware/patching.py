from typing import Any, Optional

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Overwrite


class PatchToolCallsMiddleware(AgentMiddleware):
    """修复消息历史中悬空工具调用的中间件。"""

    @staticmethod
    def _build_cancelled_tool_message(tool_call: dict[str, Any]) -> ToolMessage:
        """构造取消状态的工具响应消息。"""
        tool_name = tool_call.get("name") or "unknown_tool"
        tool_call_id = tool_call.get("id") or ""
        tool_msg = (
            f"Tool call {tool_name} with id {tool_call_id} was "
            "cancelled - another message came in before it could be completed."
        )
        return ToolMessage(
            content=tool_msg,
            name=tool_name,
            tool_call_id=tool_call_id,
        )

    @classmethod
    def _normalize_messages(cls, messages: list[BaseMessage]) -> list[BaseMessage]:
        """规范化工具调用消息顺序，满足 OpenAI tool_calls 协议要求。"""
        if not messages or len(messages) == 0:
            return messages

        tool_messages = {
            msg.tool_call_id: msg
            for msg in messages
            if isinstance(msg, ToolMessage) and msg.tool_call_id
        }
        patched_messages = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                continue

            patched_messages.append(msg)
            if not isinstance(msg, AIMessage) or not msg.tool_calls:
                continue

            for tool_call in msg.tool_calls:
                tool_call_id = tool_call.get("id")
                corresponding_tool_msg = tool_messages.get(tool_call_id)
                if corresponding_tool_msg:
                    patched_messages.append(corresponding_tool_msg)
                else:
                    patched_messages.append(cls._build_cancelled_tool_message(tool_call))

        return patched_messages

    def before_agent(self, state: AgentState, runtime: Runtime[Any]) -> Optional[dict[str, Any]]:  # noqa: ARG002
        """在代理运行之前，处理任何 AIMessage 中悬空或乱序的工具调用。"""
        messages = state["messages"]
        patched_messages = self._normalize_messages(messages)
        if patched_messages == messages:
            return None

        return {"messages": Overwrite(patched_messages)}

    async def abefore_agent(self, state: AgentState, runtime: Runtime[Any]) -> Optional[dict[str, Any]]:  # noqa: ARG002
        """在代理异步运行之前，处理任何 AIMessage 中悬空或乱序的工具调用。"""
        messages = state["messages"]
        patched_messages = self._normalize_messages(messages)
        if patched_messages == messages:
            return None

        return {"messages": Overwrite(patched_messages)}
