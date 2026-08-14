"""Agent 会话上下文压缩中间件。"""

from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.messages.utils import get_buffer_string
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from app.agent.middleware.usage import UsageMiddleware
from app.runtime.log import logger

try:
    _internal_call_metadata = import_module(
        "langchain.agents.middleware.internal_call_transformer"
    ).internal_call_metadata
except ImportError:

    def _internal_call_metadata() -> dict[str, Any]:
        """旧版 LangChain 没有内部模型调用的流式过滤标记。"""
        return {}


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
        summary_model = getattr(self, "_summary_model", self.model)
        try:
            response = summary_model.invoke(
                self.summary_prompt.format(messages=formatted_messages).rstrip(),
                config={
                    "metadata": {
                        "lc_source": "summarization",
                        **_internal_call_metadata(),
                    }
                },
            )
        except Exception as err:
            raise ContextSummarizationError(self._ERROR_MESSAGE) from err
        return self._require_valid_summary(response.text.strip())

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """异步摘要失败时保持原图状态。"""
        formatted_messages = self._prepare_summary_input(messages_to_summarize)
        summary_model = getattr(self, "_summary_model", self.model)
        try:
            response = await summary_model.ainvoke(
                self.summary_prompt.format(messages=formatted_messages).rstrip(),
                config={
                    "metadata": {
                        "lc_source": "summarization",
                        **_internal_call_metadata(),
                    }
                },
            )
        except Exception as err:
            raise ContextSummarizationError(self._ERROR_MESSAGE) from err
        return self._require_valid_summary(response.text.strip())

    def partition_for_token_limit(
        self,
        messages: list[AnyMessage],
        token_limit: int,
        *,
        force: bool = False,
        minimum_cutoff: int = 1,
        strict_token_limit: bool = False,
    ) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
        """按 token 上限拆分历史，并保持 LangChain 的工具调用事务边界。"""
        self._ensure_message_ids(messages)
        if self.token_counter(messages) <= token_limit:
            return (
                self._minimum_safe_partition(
                    messages,
                    minimum_cutoff=minimum_cutoff,
                    token_limit=token_limit if strict_token_limit else None,
                )
                if force
                else None
            )

        left, right = 0, len(messages)
        cutoff_candidate = len(messages)
        while left < right:
            midpoint = (left + right) // 2
            if self._partial_token_counter(messages[midpoint:]) <= token_limit:
                cutoff_candidate = midpoint
                right = midpoint
            else:
                left = midpoint + 1

        if cutoff_candidate >= len(messages):
            cutoff_candidate = len(messages)
        cutoff_index = self._find_safe_cutoff_point(messages, cutoff_candidate)
        if (
            cutoff_index <= 0
            or cutoff_index >= len(messages)
            or cutoff_index < minimum_cutoff
            or not self._contains_unsummarized_message(messages[:cutoff_index])
            or (
                strict_token_limit
                and self._partial_token_counter(messages[cutoff_index:]) > token_limit
            )
        ):
            if cutoff_candidate >= len(messages):
                if strict_token_limit:
                    return None
                return self._latest_safe_partition(
                    messages,
                    minimum_cutoff=minimum_cutoff,
                )
            return self._minimum_safe_partition(
                messages,
                minimum_cutoff=max(minimum_cutoff, cutoff_candidate),
                token_limit=token_limit if strict_token_limit else None,
            )
        return self._partition_messages(messages, cutoff_index)

    def partition_for_retention(
        self, messages: list[AnyMessage]
    ) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
        """按摘要器既有触发和保留策略拆分历史。"""
        self._ensure_message_ids(messages)
        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None
        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None
        return self._partition_messages(messages, cutoff_index)

    def _minimum_safe_partition(
        self,
        messages: list[AnyMessage],
        *,
        minimum_cutoff: int = 1,
        token_limit: int | None = None,
    ) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
        """至少摘要一段旧历史，同时保留最新完整消息事务。"""
        for candidate in range(max(1, minimum_cutoff), len(messages)):
            cutoff_index = self._find_safe_cutoff_point(messages, candidate)
            if (
                minimum_cutoff <= cutoff_index < len(messages)
                and self._contains_unsummarized_message(messages[:cutoff_index])
                and (
                    token_limit is None
                    or self._partial_token_counter(messages[cutoff_index:])
                    <= token_limit
                )
            ):
                return self._partition_messages(messages, cutoff_index)
        return None

    def _latest_safe_partition(
        self,
        messages: list[AnyMessage],
        *,
        minimum_cutoff: int,
    ) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
        """保留无法满足软预算时的最新完整消息事务。"""
        for candidate in range(len(messages) - 1, minimum_cutoff - 1, -1):
            cutoff_index = self._find_safe_cutoff_point(messages, candidate)
            if (
                minimum_cutoff <= cutoff_index < len(messages)
                and self._contains_unsummarized_message(messages[:cutoff_index])
            ):
                return self._partition_messages(messages, cutoff_index)
        return None

    @staticmethod
    def _contains_unsummarized_message(messages: list[AnyMessage]) -> bool:
        """确认待摘要段包含可推进上下文的原始消息。"""
        return any(
            message.additional_kwargs.get("lc_source") != "summarization"
            for message in messages
        )

    def build_summary_messages(self, summary: str) -> list[AnyMessage]:
        """将摘要转换为 LangChain 约定的可识别历史消息。"""
        return self._build_new_messages(summary)

    def ensure_message_ids(self, messages: list[AnyMessage]) -> None:
        """为压缩后消息补齐 LangGraph reducer 所需的稳定 ID。"""
        self._ensure_message_ids(messages)

    def create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """通过 MoviePilot 的失败保护合同生成同步摘要。"""
        return self._create_summary(messages_to_summarize)

    async def acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """通过 MoviePilot 的失败保护合同生成异步摘要。"""
        return await self._acreate_summary(messages_to_summarize)


class FinalRequestCompactionMiddleware(AgentMiddleware):
    """按最终模型请求预算压缩历史，并在模型成功后原子提交新状态。"""

    _COMPACTION_ANCHOR_KEY = "moviepilot_compaction_anchor_id"
    _UNCOMPRESSIBLE_REQUEST = (
        "最终模型请求压缩后仍超出上下文窗口，原有上下文已保留，"
        "请减少启用工具或切换更大上下文模型"
    )

    def __init__(
        self,
        *,
        summarizer: ContextPreservingSummarizationMiddleware,
        trigger_fraction: float = 0.85,
        keep_fraction: float = 0.10,
    ) -> None:
        self.summarizer = summarizer
        self.trigger_fraction = trigger_fraction
        self.keep_fraction = keep_fraction

    def _should_compact(self, budget: dict[str, Any]) -> bool:
        """以最终请求实际模型窗口判断是否需要压缩。"""
        estimated_tokens = budget.get("estimated_input_tokens")
        context_window = budget.get("context_window_tokens")
        return (
            isinstance(estimated_tokens, int)
            and isinstance(context_window, int)
            and estimated_tokens >= context_window * self.trigger_fraction
        )

    def _compaction_partition(
        self, request: ModelRequest
    ) -> tuple[list[AnyMessage], list[AnyMessage]] | None:
        """最终输入达到阈值时，拆分需要摘要和需要原样保留的消息。"""
        messages = list(request.messages)
        try:
            budget = UsageMiddleware.estimate_request(request)
        except Exception as error:
            logger.debug(
                "最终模型请求预算评估失败，继续原请求: error_type=%s",
                type(error).__name__,
            )
            return None

        context_window = budget.get("context_window_tokens")
        if self._should_skip_after_current_turn_compaction(messages, budget):
            return None
        if not self._should_compact(budget) or not isinstance(context_window, int):
            return None
        try:
            partition = self.summarizer.partition_for_retention(messages)
            if partition is None:
                partition = self.summarizer.partition_for_token_limit(
                    messages,
                    max(1, int(context_window * self.keep_fraction)),
                    force=budget["estimated_input_tokens"] > context_window,
                )
        except Exception as error:
            logger.debug(
                "最终请求历史拆分失败，继续原请求: error_type=%s",
                type(error).__name__,
            )
            if budget["estimated_input_tokens"] > context_window:
                raise ContextSummarizationError(
                    self._UNCOMPRESSIBLE_REQUEST
                ) from error
            return None
        if partition is None:
            if budget["estimated_input_tokens"] > context_window:
                raise ContextSummarizationError(self._UNCOMPRESSIBLE_REQUEST)
            return None
        messages_to_summarize, preserved_messages = partition
        if all(
            message.additional_kwargs.get("lc_source") == "summarization"
            for message in messages_to_summarize
        ):
            if budget["estimated_input_tokens"] > context_window:
                raise ContextSummarizationError(self._UNCOMPRESSIBLE_REQUEST)
            return None
        return messages_to_summarize, preserved_messages

    @classmethod
    def _should_skip_after_current_turn_compaction(
        cls, messages: list[AnyMessage], budget: dict[str, Any]
    ) -> bool:
        """同轮只在新工具结果已使请求超窗时再次压缩。"""
        for message in reversed(messages):
            anchor_id = message.additional_kwargs.get(cls._COMPACTION_ANCHOR_KEY)
            if not isinstance(anchor_id, str):
                continue
            anchor_index = next(
                (
                    index
                    for index, candidate in enumerate(messages)
                    if candidate.id == anchor_id
                ),
                None,
            )
            if anchor_index is None:
                return False
            messages_after_anchor = messages[anchor_index + 1 :]
            if any(
                isinstance(candidate, HumanMessage)
                for candidate in messages_after_anchor
            ):
                return False
            if any(isinstance(candidate, ToolMessage) for candidate in messages_after_anchor):
                estimated_tokens = budget.get("estimated_input_tokens")
                context_window = budget.get("context_window_tokens")
                return not (
                    isinstance(estimated_tokens, int)
                    and isinstance(context_window, int)
                    and estimated_tokens > context_window
                )
            return True
        return False

    def _build_compacted_messages(
        self, summary: str, preserved_messages: list[AnyMessage]
    ) -> list[AnyMessage]:
        """构造摘要与近期历史，并记录本轮压缩输入边界。"""
        summary_messages = self.summarizer.build_summary_messages(summary)
        if summary_messages:
            self.summarizer.ensure_message_ids(summary_messages)
            anchor_id = (
                preserved_messages[-1].id
                if preserved_messages
                else summary_messages[0].id
            )
            first_summary = summary_messages[0]
            summary_messages[0] = first_summary.model_copy(
                update={
                    "additional_kwargs": {
                        **first_summary.additional_kwargs,
                        self._COMPACTION_ANCHOR_KEY: anchor_id,
                    }
                }
            )
        return [*summary_messages, *preserved_messages]

    def _validate_or_repartition(
        self,
        request: ModelRequest,
        messages_to_summarize: list[AnyMessage],
        preserved_messages: list[AnyMessage],
        summary: str,
    ) -> tuple[list[AnyMessage], tuple[list[AnyMessage], list[AnyMessage]] | None]:
        """复核压缩后的最终预算，并计算一次更小的近期历史分区。"""
        compacted_messages = self._build_compacted_messages(summary, preserved_messages)
        compacted_budget = UsageMiddleware.estimate_request(
            request.override(messages=compacted_messages)
        )
        context_window = compacted_budget.get("context_window_tokens")
        estimated_tokens = compacted_budget.get("estimated_input_tokens")
        if not isinstance(context_window, int) or not isinstance(estimated_tokens, int):
            return compacted_messages, None

        if estimated_tokens <= context_window:
            return compacted_messages, None

        summary_messages = self.summarizer.build_summary_messages(summary)
        fixed_summary_budget = UsageMiddleware.estimate_request(
            request.override(messages=summary_messages)
        )
        available_recent_tokens = (
            context_window - fixed_summary_budget["estimated_input_tokens"]
        )
        if available_recent_tokens <= 0:
            raise ContextSummarizationError(self._UNCOMPRESSIBLE_REQUEST)
        repartition = self.summarizer.partition_for_token_limit(
            list(request.messages),
            available_recent_tokens,
            force=True,
            minimum_cutoff=len(messages_to_summarize) + 1,
            strict_token_limit=True,
        )
        if (
            repartition is None
            or len(repartition[0]) <= len(messages_to_summarize)
        ):
            raise ContextSummarizationError(self._UNCOMPRESSIBLE_REQUEST)
        return compacted_messages, repartition

    def _require_within_window(
        self, request: ModelRequest, compacted_messages: list[AnyMessage]
    ) -> None:
        """禁止把已知仍超过主模型窗口的请求发送给 provider。"""
        budget = UsageMiddleware.estimate_request(
            request.override(messages=compacted_messages)
        )
        estimated_tokens = budget.get("estimated_input_tokens")
        context_window = budget.get("context_window_tokens")
        if (
            isinstance(estimated_tokens, int)
            and isinstance(context_window, int)
            and estimated_tokens > context_window
        ):
            raise ContextSummarizationError(self._UNCOMPRESSIBLE_REQUEST)

    def _prepare_messages(self, request: ModelRequest) -> list[AnyMessage] | None:
        """同步生成摘要与需要原样保留的近期消息。"""
        partition = self._compaction_partition(request)
        if partition is None:
            return None
        messages_to_summarize, preserved_messages = partition
        summary = self.summarizer.create_summary(messages_to_summarize)
        compacted_messages, repartition = self._validate_or_repartition(
            request,
            messages_to_summarize,
            preserved_messages,
            summary,
        )
        if repartition is not None:
            messages_to_summarize, preserved_messages = repartition
            summary = self.summarizer.create_summary(messages_to_summarize)
            compacted_messages = self._build_compacted_messages(
                summary, preserved_messages
            )
        self._require_within_window(request, compacted_messages)
        return compacted_messages

    async def _aprepare_messages(
        self, request: ModelRequest
    ) -> list[AnyMessage] | None:
        """异步生成摘要与需要原样保留的近期消息。"""
        partition = self._compaction_partition(request)
        if partition is None:
            return None
        messages_to_summarize, preserved_messages = partition
        summary = await self.summarizer.acreate_summary(messages_to_summarize)
        compacted_messages, repartition = self._validate_or_repartition(
            request,
            messages_to_summarize,
            preserved_messages,
            summary,
        )
        if repartition is not None:
            messages_to_summarize, preserved_messages = repartition
            summary = await self.summarizer.acreate_summary(messages_to_summarize)
            compacted_messages = self._build_compacted_messages(
                summary, preserved_messages
            )
        self._require_within_window(request, compacted_messages)
        return compacted_messages

    @staticmethod
    def _with_state_update(
        response: ModelResponse, compacted_messages: list[AnyMessage]
    ) -> ExtendedModelResponse:
        """主模型成功后一次性替换历史，同时保留本次模型结果。"""
        return ExtendedModelResponse(
            model_response=response,
            command=Command(
                update={
                    "messages": [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        *compacted_messages,
                        *response.result,
                    ]
                }
            ),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """同步压缩最终请求；模型失败时不提交摘要状态。"""
        compacted_messages = self._prepare_messages(request)
        if compacted_messages is None:
            return handler(request)
        response = handler(request.override(messages=compacted_messages))
        return self._with_state_update(response, compacted_messages)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """异步压缩最终请求；模型失败时不提交摘要状态。"""
        compacted_messages = await self._aprepare_messages(request)
        if compacted_messages is None:
            return await handler(request)
        response = await handler(request.override(messages=compacted_messages))
        return self._with_state_update(response, compacted_messages)
