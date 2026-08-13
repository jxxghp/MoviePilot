"""Agent 会话上下文压缩中间件。"""

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AnyMessage
from langchain_core.messages.utils import get_buffer_string


class ContextSummarizationError(RuntimeError):
    """摘要不可用且原有会话上下文未被替换。"""


class ContextPreservingSummarizationMiddleware(SummarizationMiddleware):
    """摘要失败时中止状态更新，避免永久丢失既有会话上下文。"""

    _ERROR_MESSAGE = "会话上下文压缩失败，原有上下文已保留，请稍后重试"
    _UNSUMMARIZABLE_MESSAGE = (
        "会话历史中存在无法压缩的超长内容，原有上下文已保留，"
        "请新建或清空会话后继续"
    )

    @classmethod
    def _require_valid_summary(cls, summary: str) -> str:
        """拒绝无法继续承载会话上下文的空摘要。"""
        if not summary:
            raise ContextSummarizationError(cls._ERROR_MESSAGE)
        return summary

    def _prepare_summary_input(
        self, messages_to_summarize: list[AnyMessage]
    ) -> str:
        """复用 LangChain 裁剪策略生成摘要模型输入。"""
        trimmed_messages = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed_messages:
            raise ContextSummarizationError(self._UNSUMMARIZABLE_MESSAGE)
        return get_buffer_string(trimmed_messages, format="xml")

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """同步摘要失败时保持原图状态。"""
        formatted_messages = self._prepare_summary_input(messages_to_summarize)
        try:
            response = self.model.invoke(
                self.summary_prompt.format(messages=formatted_messages).rstrip(),
                config={"metadata": {"lc_source": "summarization"}},
            )
        except Exception as err:
            raise ContextSummarizationError(self._ERROR_MESSAGE) from err
        return self._require_valid_summary(response.text.strip())

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """异步摘要失败时保持原图状态。"""
        formatted_messages = self._prepare_summary_input(messages_to_summarize)
        try:
            response = await self.model.ainvoke(
                self.summary_prompt.format(messages=formatted_messages).rstrip(),
                config={"metadata": {"lc_source": "summarization"}},
            )
        except Exception as err:
            raise ContextSummarizationError(self._ERROR_MESSAGE) from err
        return self._require_valid_summary(response.text.strip())
