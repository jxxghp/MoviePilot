"""把运行中用户消息作为真实 HumanMessage 注入下一次模型调用。"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.steering import SteeringMessage, current_steering_inbox

STEERING_MESSAGE_ID_KEY = "moviepilot_steering_message_id"
STEERING_CONTEXT_KEY = "continuation_context"
STEERING_CONTEXT_INSTRUCTION = (
    "这是当前运行任务的补充要求。继续原任务并保留此前的范围、停止条件、确认标准、输出格式和安全约束；"
    "只有用户明确修改时才改变它们。"
)


def _to_human_message(message: SteeringMessage) -> HumanMessage:
    """将 steering 传输对象转换成可持久化、可审计的标准用户消息。"""
    payload = {
        "message": message.text,
        "input": {"mode": "text", "steering": True},
        STEERING_CONTEXT_KEY: STEERING_CONTEXT_INSTRUCTION,
        "images": [{"index": index + 1, "type": "image"} for index, _ in enumerate(message.images)],
        "files": list(message.files),
    }
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, indent=2),
        }
    ]
    content.extend({"type": "image_url", "image_url": {"url": image}} for image in message.images)
    return HumanMessage(
        content=content,
        additional_kwargs={STEERING_MESSAGE_ID_KEY: message.message_id},
    )


class SteeringMiddleware(AgentMiddleware):  # type: ignore[misc]
    """在模型请求边界消费会话 inbox，避免并发启动第二条 Agent 图。"""

    @staticmethod
    def _can_inject(messages: list[Any]) -> bool:
        """工具调用尚未交付时不插入用户消息，保持 AI/Tool 配对协议完整。"""
        return not (messages and isinstance(messages[-1], AIMessage) and bool(messages[-1].tool_calls))

    @staticmethod
    def _append_messages(
        request: ModelRequest[Any],
        messages: tuple[SteeringMessage, ...],
    ) -> ModelRequest[Any]:
        """把已消费消息写入图状态和本次模型请求，确保可持久化且只出现一次。"""
        if not messages:
            return request
        injected = [_to_human_message(message) for message in messages]
        request_messages = list(request.messages)
        state_messages = request.state.get("messages")
        if isinstance(state_messages, list):
            state_messages.extend(injected)
        request_messages.extend(injected)
        return request.override(messages=request_messages)

    def _prepare_request(
        self,
        request: ModelRequest[Any],
        consume: Callable[[], tuple[SteeringMessage, ...]],
    ) -> ModelRequest[Any]:
        """同步消费并装配消息；工具调用未完成时保留队列等待下一次请求。"""
        if not self._can_inject(list(request.messages)):
            return request
        inbox = current_steering_inbox()
        messages = consume() if inbox is not None else ()
        return self._append_messages(request, messages)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """同步模型调用前注入真实 HumanMessage，不增加 LangGraph 递归节点。"""
        inbox = current_steering_inbox()
        consume = inbox.consume_nowait if inbox is not None else lambda: ()
        return handler(self._prepare_request(request, consume))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """异步模型调用前原子注入真实 HumanMessage，不启动并行 Agent。"""
        inbox = current_steering_inbox()
        if not self._can_inject(list(request.messages)) or inbox is None:
            return await handler(request)
        return await handler(self._append_messages(request, await inbox.consume()))


__all__ = ["STEERING_MESSAGE_ID_KEY", "SteeringMiddleware"]
