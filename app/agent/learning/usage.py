"""复盘所有模型请求的统一计量，包括独立上下文压缩的辅助请求。"""

from typing import Any, Callable
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import LLMResult

from app.agent.middleware.usage import UsageMiddleware


class ReviewUsage(BaseCallbackHandler):  # type: ignore[misc]
    """按 SDK 请求 ID 结算实际输入 token，缺少 usage 时使用开始时的保守估算。"""

    run_inline = True

    def __init__(self, on_usage: Callable[[dict[str, Any]], None]) -> None:
        """私有回调不继承前台 streaming handler，不能把复盘文本推给用户。"""
        self.on_usage = on_usage
        self.consumed = 0
        self.pending: dict[UUID, int] = {}

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID, **kwargs: Any) -> None:
        """计量预估包含工具 schema 序列化，失败请求也保守占用复盘预算。"""
        del serialized
        schema = str(kwargs.get('invocation_params', {}).get('tools', []))
        self.pending[run_id] = max(1, int(sum(count_tokens_approximately(batch) for batch in messages) + len(schema) / 4))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """计入压缩及主复盘回复的真实账单，父会话只累计用量而不替换最近请求快照。"""
        del kwargs
        estimate = self.pending.pop(run_id, 1)
        message = next((generation.message for group in response.generations for generation in group
                        if isinstance(getattr(generation, 'message', None), AIMessage)), None)
        usage = UsageMiddleware._extract_usage(message) if message is not None else {}
        self.consumed += max(1, usage.get('input_tokens') or estimate)
        self.on_usage({**usage, 'source': 'background_review'})

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """供应商未给账单的失败请求按估算占预算，不伪造真实已计费 usage。"""
        del error, kwargs
        self.consumed += self.pending.pop(run_id, 1)
