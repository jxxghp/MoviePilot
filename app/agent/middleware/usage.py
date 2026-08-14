from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.runtime.log import logger


class UsageMiddleware(AgentMiddleware):
    """观察最终模型请求预算，并记录模型返回的真实 usage。"""

    def __init__(
        self,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        on_request_budget: Callable[[dict[str, Any]], None] | None = None,
        next_request_sequence: Callable[[], int] | None = None,
    ) -> None:
        self.on_usage = on_usage
        self.on_request_budget = on_request_budget
        self.next_request_sequence = next_request_sequence
        self._request_sequence = 0

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        """仅接受模型 profile 和请求设置声明的非 bool 正整数。"""
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    @classmethod
    def _lookup_positive_int(cls, container: Any, *keys: str) -> int | None:
        """按字段优先级读取 token 上限，拒绝隐式数值转换。"""
        if not container:
            return None

        getter = getattr(container, "get", None)
        if callable(getter):
            for key in keys:
                value = getter(key)
                if value is not None:
                    normalized = cls._coerce_positive_int(value)
                    if normalized is not None:
                        return normalized

        for key in keys:
            value = getattr(container, key, None)
            if value is not None:
                normalized = cls._coerce_positive_int(value)
                if normalized is not None:
                    return normalized

        return None

    @classmethod
    def _lookup_int(cls, container: Any, *keys: str) -> int | None:
        if not container:
            return None

        getter = getattr(container, "get", None)
        if callable(getter):
            for key in keys:
                value = getter(key)
                if value is not None:
                    return cls._coerce_int(value)

        for key in keys:
            value = getattr(container, key, None)
            if value is not None:
                return cls._coerce_int(value)

        return None

    @classmethod
    def _first_int(
        cls,
        candidates: tuple[tuple[Any, tuple[str, ...]], ...],
    ) -> int | None:
        """按优先级返回首个可用的 usage 整数值。"""
        for container, keys in candidates:
            value = cls._lookup_int(container, *keys)
            if value is not None:
                return value
        return None

    @classmethod
    def _extract_model_name(cls, model: Any) -> str | None:
        for field in ("model", "model_name", "model_id"):
            try:
                value = getattr(model, field, None)
            except Exception:
                continue
            if value:
                return value
        return None

    @classmethod
    def _extract_context_window_tokens(cls, model: Any) -> int | None:
        try:
            profile = getattr(model, "profile", None)
        except Exception:
            return None
        if not profile:
            return None
        try:
            return cls._lookup_positive_int(
                profile, "max_input_tokens", "input_token_limit"
            )
        except Exception:
            return None

    @classmethod
    def _extract_model_max_output_tokens(cls, model: Any) -> int | None:
        """读取模型输出能力上限；该值不代表单次请求已经预留的输出空间。"""
        try:
            profile = getattr(model, "profile", None)
        except Exception:
            return None
        if not profile:
            return None
        try:
            return cls._lookup_positive_int(
                profile, "max_output_tokens", "output_token_limit"
            )
        except Exception:
            return None

    def _next_request_sequence(self) -> int | None:
        """优先使用会话级序号，使图重建后的请求仍保持单调顺序。"""
        if callable(self.next_request_sequence):
            try:
                return self.next_request_sequence()
            except Exception as error:
                logger.debug(
                    "分配会话级模型请求序号失败: error_type=%s",
                    type(error).__name__,
                )
                # 无法证明顺序的请求仍可累计 usage，但不能参与最近请求快照竞争。
                return None
        self._request_sequence += 1
        return self._request_sequence

    @classmethod
    def _extract_configured_output_limit_tokens(
        cls, request: ModelRequest
    ) -> int | None:
        """读取最终请求显式配置的单次输出上限。"""
        model_settings = request.model_settings or {}
        value = cls._lookup_positive_int(
            model_settings,
            "max_completion_tokens",
            "max_tokens",
            "max_output_tokens",
        )
        return value

    @staticmethod
    def _count_multimodal_blocks(messages: list[Any]) -> tuple[int, int]:
        """统计图片和未知多模态块，不保留块内容或资源地址。"""
        image_count = 0
        unknown_count = 0
        for message in messages:
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, str):
                    continue
                if not isinstance(block, dict):
                    unknown_count += 1
                    continue
                block_type = block.get("type")
                if block_type in {"image", "image_url"}:
                    image_count += 1
                elif block_type != "text":
                    unknown_count += 1
        return image_count, unknown_count

    @classmethod
    def estimate_request(cls, request: ModelRequest) -> dict[str, Any]:
        """估算最终模型输入组成，仅返回可安全暴露的聚合数字。"""
        messages = list(request.messages or [])
        system_messages = [request.system_message] if request.system_message else []
        tools = list(request.tools or [])
        message_tokens = count_tokens_approximately(
            messages,
            use_usage_metadata_scaling=False,
        )
        system_tokens = count_tokens_approximately(
            system_messages,
            use_usage_metadata_scaling=False,
        )
        tool_tokens = count_tokens_approximately(
            [],
            tools=tools,
            use_usage_metadata_scaling=False,
        )
        estimated_input_tokens = message_tokens + system_tokens + tool_tokens
        context_window_tokens = cls._extract_context_window_tokens(request.model)
        model_max_output_tokens = cls._extract_model_max_output_tokens(request.model)
        configured_output_limit_tokens = cls._extract_configured_output_limit_tokens(
            request
        )
        image_count, unknown_multimodal_count = cls._count_multimodal_blocks(
            [*system_messages, *messages]
        )
        estimated_input_ratio = (
            estimated_input_tokens / context_window_tokens
            if context_window_tokens
            else None
        )
        return {
            "has_estimate": True,
            "model": cls._extract_model_name(request.model),
            "message_count": len(messages),
            "tool_count": len(tools),
            "image_count": image_count,
            "unknown_multimodal_count": unknown_multimodal_count,
            "message_tokens": message_tokens,
            "system_tokens": system_tokens,
            "tool_tokens": tool_tokens,
            # 该成本已经包含在 message_tokens 中，只单独暴露组成，不能再次汇总。
            "multimodal_tokens": image_count * 85,
            "estimated_input_tokens": estimated_input_tokens,
            "context_window_tokens": context_window_tokens,
            "estimated_remaining_input_tokens": (
                context_window_tokens - estimated_input_tokens
                if context_window_tokens
                else None
            ),
            "estimated_input_ratio": estimated_input_ratio,
            "estimated_over_input_limit": (
                estimated_input_tokens > context_window_tokens
                if context_window_tokens
                else None
            ),
            "model_max_output_tokens": model_max_output_tokens,
            "configured_output_limit_tokens": configured_output_limit_tokens,
        }

    @classmethod
    def _extract_usage(cls, ai_message: AIMessage) -> dict[str, Any]:
        usage_metadata = getattr(ai_message, "usage_metadata", None)

        input_tokens = cls._lookup_int(usage_metadata, "input_tokens")
        output_tokens = cls._lookup_int(usage_metadata, "output_tokens")
        total_tokens = cls._lookup_int(usage_metadata, "total_tokens")

        response_metadata = getattr(ai_message, "response_metadata", None) or {}
        token_usage = (
            response_metadata.get("token_usage")
            or response_metadata.get("usage")
            or response_metadata.get("usage_metadata")
            or {}
        )

        input_token_details = None
        if usage_metadata:
            getter = getattr(usage_metadata, "get", None)
            input_token_details = (
                getter("input_token_details")
                if callable(getter)
                else getattr(usage_metadata, "input_token_details", None)
            )

        cache_read_tokens = cls._first_int(
            (
                (
                    input_token_details,
                    (
                        "cache_read",
                        "cached_tokens",
                        "cache_read_input_tokens",
                        "cacheReadInputTokens",
                    ),
                ),
                (
                    token_usage,
                    (
                        "prompt_cache_hit_tokens",
                        "cache_read_input_tokens",
                        "cacheReadInputTokens",
                    ),
                ),
                (
                    response_metadata,
                    (
                        "prompt_cache_hit_tokens",
                        "cache_read_input_tokens",
                        "cacheReadInputTokens",
                        "cached_tokens",
                    ),
                ),
            )
        )
        if cache_read_tokens is None:
            cache_read_tokens = cls._first_int(
                (
                    (
                        token_usage.get("prompt_tokens_details", {}),
                        ("cached_tokens", "cache_read"),
                    ),
                    (
                        token_usage.get("input_tokens_details", {}),
                        ("cached_tokens", "cache_read"),
                    ),
                )
            )

        cache_write_tokens = cls._first_int(
            (
                (
                    input_token_details,
                    (
                        "cache_creation",
                        "cache_write",
                        "cache_write_tokens",
                        "cache_write_input_tokens",
                        "cacheWriteInputTokens",
                    ),
                ),
                (
                    token_usage,
                    (
                        "cache_creation_input_tokens",
                        "cache_write_tokens",
                        "cache_write_input_tokens",
                        "cacheWriteInputTokens",
                    ),
                ),
                (
                    response_metadata,
                    (
                        "cache_creation_input_tokens",
                        "cache_write_tokens",
                        "cache_write_input_tokens",
                        "cacheWriteInputTokens",
                    ),
                ),
            )
        )
        if cache_write_tokens is None:
            cache_write_tokens = cls._first_int(
                (
                    (
                        token_usage.get("prompt_tokens_details", {}),
                        ("cache_write_tokens", "cache_creation"),
                    ),
                    (
                        token_usage.get("input_tokens_details", {}),
                        ("cache_write_tokens", "cache_creation"),
                    ),
                )
            )
        cache_write_ttl_tokens = sum(
            cls._lookup_int(
                input_token_details,
                ttl_key,
            )
            or 0
            for ttl_key in (
                "ephemeral_5m_input_tokens",
                "ephemeral_1h_input_tokens",
            )
        )
        if cache_write_ttl_tokens:
            cache_write_tokens = cache_write_ttl_tokens

        cache_miss_tokens = cls._first_int(
            (
                (
                    token_usage,
                    ("prompt_cache_miss_tokens", "cache_miss_input_tokens"),
                ),
                (
                    response_metadata,
                    ("prompt_cache_miss_tokens", "cache_miss_input_tokens"),
                ),
            )
        )

        if input_tokens is None:
            input_tokens = cls._lookup_int(
                token_usage,
                "prompt_tokens",
                "input_tokens",
            )
        if input_tokens is None:
            input_tokens = cls._lookup_int(
                response_metadata,
                "prompt_token_count",
                "input_tokens",
            )
        if input_tokens is None:
            bedrock_input_tokens = cls._lookup_int(token_usage, "inputTokens")
            if bedrock_input_tokens is not None:
                input_tokens = (
                    bedrock_input_tokens
                    + (cache_read_tokens or 0)
                    + (cache_write_tokens or 0)
                )
        if input_tokens is None and any(
            value is not None
            for value in (
                cache_read_tokens,
                cache_write_tokens,
                cache_miss_tokens,
            )
        ):
            input_tokens = (
                (cache_read_tokens or 0)
                + (cache_write_tokens or 0)
                + (cache_miss_tokens or 0)
            )

        if output_tokens is None:
            output_tokens = cls._lookup_int(
                token_usage,
                "completion_tokens",
                "output_tokens",
            )
        if output_tokens is None:
            output_tokens = cls._lookup_int(
                response_metadata,
                "candidates_token_count",
                "output_tokens",
            )

        if total_tokens is None:
            total_tokens = cls._lookup_int(token_usage, "total_tokens")
        if total_tokens is None:
            total_tokens = cls._lookup_int(response_metadata, "total_token_count")

        has_cache_usage = any(
            value is not None
            for value in (
                cache_read_tokens,
                cache_write_tokens,
                cache_miss_tokens,
            )
        )
        has_usage = any(
            value is not None
            for value in (
                input_tokens,
                output_tokens,
                total_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cache_miss_tokens,
            )
        )
        input_usage_available = input_tokens is not None
        resolved_input = input_tokens or 0
        resolved_output = output_tokens or 0
        resolved_total = (
            total_tokens
            if total_tokens is not None
            else resolved_input + resolved_output
        )
        resolved_cache_read = cache_read_tokens or 0
        resolved_cache_write = cache_write_tokens or 0
        uncached_input_tokens = (
            cache_miss_tokens
            if cache_miss_tokens is not None
            else max(
                resolved_input - resolved_cache_read - resolved_cache_write,
                0,
            )
        )
        cache_hit_ratio = (
            resolved_cache_read / resolved_input
            if has_cache_usage and resolved_input
            else None
        )

        return {
            "has_usage": has_usage,
            "input_usage_available": input_usage_available,
            "cache_usage_available": has_cache_usage,
            "input_tokens": resolved_input,
            "output_tokens": resolved_output,
            "total_tokens": resolved_total,
            "cache_read_input_tokens": resolved_cache_read,
            "cache_write_input_tokens": resolved_cache_write,
            "uncached_input_tokens": uncached_input_tokens,
            "cache_hit_ratio": cache_hit_ratio,
        }

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        request_sequence = self._next_request_sequence()
        request_budget = None
        try:
            request_budget = {
                "request_sequence": request_sequence,
                **self.estimate_request(request),
            }
        except Exception as error:
            logger.debug(
                "估算最终模型请求预算失败: error_type=%s",
                type(error).__name__,
            )
            request_budget = {
                "request_sequence": request_sequence,
                "has_estimate": False,
                "model": self._extract_model_name(request.model),
                "context_window_tokens": self._extract_context_window_tokens(
                    request.model
                ),
            }
        if callable(self.on_request_budget):
            request_budget_recorded = False
            try:
                self.on_request_budget(request_budget)
                request_budget_recorded = True
            except Exception as error:
                logger.debug(
                    "记录最终模型请求预算失败: error_type=%s",
                    type(error).__name__,
                )
        else:
            request_budget_recorded = False

        response = await handler(request)

        if not callable(self.on_usage):
            return response

        try:
            ai_message = next(
                (
                    message
                    for message in reversed(response.result)
                    if isinstance(message, AIMessage)
                ),
                None,
            )
            usage = (
                self._extract_usage(ai_message)
                if ai_message
                else {
                    "has_usage": False,
                    "input_usage_available": False,
                    "cache_usage_available": False,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "uncached_input_tokens": 0,
                    "cache_hit_ratio": None,
                }
            )
            context_window_tokens = self._extract_context_window_tokens(request.model)
            context_usage_ratio = None
            if context_window_tokens and usage["input_usage_available"]:
                context_usage_ratio = usage["input_tokens"] / context_window_tokens

            self.on_usage(
                {
                    "request_sequence": request_sequence,
                    "request_budget_recorded": request_budget_recorded,
                    "estimated_input_tokens": (
                        request_budget.get("estimated_input_tokens")
                        if request_budget
                        else None
                    ),
                    "model": self._extract_model_name(request.model),
                    "context_window_tokens": context_window_tokens,
                    "context_usage_ratio": context_usage_ratio,
                    **usage,
                }
            )
        except Exception as e:
            logger.debug("记录模型 usage 失败: %s", e)

        return response


__all__ = ["UsageMiddleware"]
