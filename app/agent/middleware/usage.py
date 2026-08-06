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

from app.log import logger


class UsageMiddleware(AgentMiddleware):
    """记录模型调用 usage 信息并回传给外部会话。"""

    def __init__(
        self,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.on_usage = on_usage

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
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
        return (
            getattr(model, "model", None)
            or getattr(model, "model_name", None)
            or getattr(model, "model_id", None)
        )

    @classmethod
    def _extract_context_window_tokens(cls, model: Any) -> int | None:
        profile = getattr(model, "profile", None)
        if not profile:
            return None
        return cls._lookup_int(profile, "max_input_tokens", "input_token_limit")

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
            if context_window_tokens and usage["has_usage"]:
                context_usage_ratio = usage["input_tokens"] / context_window_tokens

            self.on_usage(
                {
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
