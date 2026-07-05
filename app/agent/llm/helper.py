"""LLM模型相关辅助功能"""

import asyncio
import inspect
import json
import time
from functools import wraps
from typing import Any, List, Optional

from langchain_core.messages import AIMessage, AIMessageChunk

from app.core.config import settings
from app.log import logger


class LLMTestError(RuntimeError):
    """LLM 测试调用异常，附带请求耗时。"""

    def __init__(self, message: str, duration_ms: int | None = None):
        super().__init__(message)
        self.duration_ms = duration_ms


class LLMTestTimeout(TimeoutError):
    """LLM 测试调用超时，附带请求耗时。"""

    def __init__(self, message: str, duration_ms: int | None = None):
        super().__init__(message)
        self.duration_ms = duration_ms


def _patch_gemini_thought_signature():
    """
    修复 langchain-google-genai 中 Gemini 2.5 思考模型的 thought_signature 兼容问题。

    问题 1：_is_gemini_3_or_later() 仅检查 "gemini-3"，不包含 Gemini 2.5 模型，
    导致 _parse_chat_history 的 thought_signature 强制注入逻辑被跳过。

    问题 2：强制注入逻辑使用 first_fc_seen 标志，只给每个 model 消息中
    第一个缺少 thought_signature 的 function_call 补 dummy，后续并行
    function_call 仍缺失签名，导致 Gemini API 返回 400。

    此补丁同时修复以上两个问题。
    """
    try:
        import langchain_google_genai.chat_models as _cm

        # 检查版本：需要 >= 4.0 才支持 _is_gemini_3_or_later
        try:
            from importlib.metadata import version
            _version = version("langchain-google-genai") or ""
        except Exception:
            _version = ""
        try:
            _major = int(_version.split(".")[0]) if _version else 0
        except (ValueError, TypeError):
            _major = 0
        if _major < 4:
            logger.error(
                f"langchain-google-genai 版本 {_version or '未知'} 过旧，"
                f"不支持 Gemini 2.5+ 模型的 thought_signature 处理，"
                f"请升级到 4.2.3+：pip install langchain-google-genai~=4.2.3"
            )
            return

        # 仅在未修补时执行
        if getattr(_cm, "_thought_signature_patched", False):
            return

        if not hasattr(_cm, "_is_gemini_3_or_later"):
            logger.error(
                "langchain-google-genai 缺少 _is_gemini_3_or_later，"
                "无法修补 thought_signature 兼容性，请检查包版本"
            )
            return

        # 补丁 1：扩展 _is_gemini_3_or_later，使 Gemini 2.5 模型也能触发
        # _parse_chat_history 中的 thought_signature 强制注入逻辑
        def _patched_is_gemini_3_or_later(model_name: str) -> bool:
            if not model_name:
                return False
            name = model_name.lower().replace("models/", "")
            return "gemini-3" in name or "gemini-2.5" in name

        _cm._is_gemini_3_or_later = _patched_is_gemini_3_or_later

        # 补丁 2：修复 _parse_chat_history 中 first_fc_seen 只修复第一个
        # function_call 的问题。用 wrapper 在原函数返回后，确保所有 model
        # 消息中所有 function_call 都带有 thought_signature。
        _original_parse_chat_history = _cm._parse_chat_history  # noqa

        def _patched_parse_chat_history(*args, **kwargs):
            result = _original_parse_chat_history(*args, **kwargs)
            system_instruction, formatted_messages = result

            # 从参数中提取 model 名称
            model = kwargs.get("model")
            if model is None and len(args) >= 4:
                model = args[3]

            if model and _patched_is_gemini_3_or_later(model):
                dummy = _cm.DUMMY_THOUGHT_SIGNATURE
                for content_msg in formatted_messages:
                    if content_msg.role == "model":
                        for part in content_msg.parts or []:
                            if part.function_call and not part.thought_signature:
                                part.thought_signature = dummy

            return result

        _cm._parse_chat_history = _patched_parse_chat_history
        _cm._thought_signature_patched = True
        logger.debug(
            "已修补 langchain-google-genai thought_signature 兼容性"
            "（覆盖 Gemini 2.5 模型 + 修复并行 function_call 签名缺失）"
        )
    except Exception as e:
        logger.warning(f"修补 langchain-google-genai thought_signature 失败: {e}")


def _get_httpx_proxy_key() -> str:
    """
    获取当前 httpx 版本支持的代理参数名。
    httpx < 0.28 使用 "proxies"（复数），>= 0.28 使用 "proxy"（单数）。
    google-genai SDK 会静默过滤掉不在 httpx.Client.__init__ 签名中的参数，
    因此必须使用与当前 httpx 版本匹配的参数名。
    """
    try:
        import httpx

        params = inspect.signature(httpx.Client.__init__).parameters
        if "proxy" in params:
            return "proxy"
        return "proxies"
    except Exception as e:
        logger.warning(f"检测 httpx 代理参数失败，默认使用 'proxies'：{e}")
        return "proxies"


def _resolve_llm_proxy(use_proxy: bool | None = None) -> str | None:
    """
    解析本次 LLM 调用应使用的系统代理地址。
    """
    should_use_proxy = settings.LLM_USE_PROXY if use_proxy is None else use_proxy
    return settings.PROXY_HOST if should_use_proxy and settings.PROXY_HOST else None


def _build_httpx_proxy_kwargs(proxy_url: str | None) -> dict[str, str]:
    """
    构造兼容当前 httpx 版本的代理参数。
    """
    if not proxy_url:
        return {}
    return {_get_httpx_proxy_key(): proxy_url}


def _build_google_client_args(proxy_url: str | None) -> dict[str, Any]:
    """
    构造 Google SDK 透传给 httpx 的客户端参数。
    """
    return {
        "trust_env": False,
        **_build_httpx_proxy_kwargs(proxy_url),
    }


def _build_httpx_client(
        proxy_url: str | None,
        *,
        async_client: bool = False,
        timeout: float | None = None,
):
    """
    构造显式代理策略的 httpx 客户端。

    当关闭 LLM 代理时也返回 trust_env=False 的客户端，避免 httpx 自动读取
    进程环境变量中的代理配置。
    """
    import httpx

    client_cls = httpx.AsyncClient if async_client else httpx.Client
    kwargs: dict[str, Any] = {
        "trust_env": False,
        **_build_httpx_proxy_kwargs(proxy_url),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client_cls(**kwargs)


def _deepseek_thinking_toggle(extra_body: Any) -> bool | None:
    """
    解析 DeepSeek extra_body 中显式传入的 thinking 开关。
    """
    if not isinstance(extra_body, dict):
        return None

    thinking = extra_body.get("thinking")
    if not isinstance(thinking, dict):
        return None

    thinking_type = str(thinking.get("type") or "").strip().lower()
    if thinking_type == "enabled":
        return True
    if thinking_type == "disabled":
        return False
    return None


def _is_deepseek_thinking_enabled(model_name: str | None, extra_body: Any) -> bool:
    """
    判断本次 DeepSeek 调用是否处于 thinking mode。
    """
    explicit_toggle = _deepseek_thinking_toggle(extra_body)
    if explicit_toggle is not None:
        return explicit_toggle

    normalized_model_name = str(model_name or "").strip().lower()
    if normalized_model_name == "deepseek-reasoner":
        return True
    if normalized_model_name.startswith("deepseek-v4-"):
        # DeepSeek V4 默认启用 thinking mode，除非显式关闭。
        return True
    return False


def _patch_deepseek_reasoning_content_support():
    """
    修补 langchain-deepseek 在 tool-call 场景下遗漏 reasoning_content 回传的问题。

    DeepSeek thinking mode 要求：若 assistant 历史消息包含 tool_calls，
    后续请求中必须带回该条消息的顶层 reasoning_content。
    某些 langchain-deepseek 版本虽然能从响应中拿到 reasoning_content，
    但不会在重放消息历史时写回请求载荷，导致 400。
    """
    try:
        from langchain_deepseek import ChatDeepSeek
    except Exception as err:
        logger.debug(f"跳过 langchain-deepseek reasoning_content 修补：{err}")
        return

    if getattr(ChatDeepSeek, "_moviepilot_reasoning_content_patched", False):
        return

    original_get_request_payload = getattr(ChatDeepSeek, "_get_request_payload", None)
    if not callable(original_get_request_payload):
        logger.warning("langchain-deepseek 缺少 _get_request_payload，无法修补 reasoning_content")
        return

    @wraps(original_get_request_payload)
    def _patched_get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = original_get_request_payload(self, input_, stop=stop, **kwargs)

        extra_body = (getattr(self, "model_kwargs", None) or {}).get("extra_body")
        if not _is_deepseek_thinking_enabled(
                getattr(self, "model_name", None) or getattr(self, "model", None),
                extra_body,
        ):
            return payload

        # 从原始 LangChain 消息中取回 reasoning_content。上游 payload 构造器
        # 不会自动透传这个 DeepSeek 扩展字段。
        messages = self._convert_input(input_).to_messages()

        for i, message in enumerate(payload["messages"]):
            if message["role"] == "tool" and isinstance(message["content"], list):
                message["content"] = json.dumps(message["content"])
            elif message["role"] == "assistant":
                if isinstance(message["content"], list):
                    # DeepSeek API 要求 assistant content 为字符串；工具场景下
                    # LangChain 可能保留为内容块列表，这里只拼回可见文本块。
                    text_parts = [
                        block.get("text", "")
                        for block in message["content"]
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    message["content"] = "".join(text_parts) if text_parts else ""

                # DeepSeek thinking mode 要求历史 assistant 消息携带
                # reasoning_content，即便本地只保存到了 additional_kwargs。
                if (
                        "reasoning_content" not in message
                        and i < len(messages)
                        and isinstance(messages[i], AIMessage)
                ):
                    message["reasoning_content"] = messages[i].additional_kwargs.get(
                        "reasoning_content", ""
                    )

        return payload

    ChatDeepSeek._get_request_payload = _patched_get_request_payload
    ChatDeepSeek._moviepilot_reasoning_content_patched = True
    logger.debug("已修补 langchain-deepseek thinking tool-call 的 reasoning_content 回传兼容性")


def _patch_openai_interleaved_reasoning_content_support():
    """
    修补 OpenAI-compatible 模型的 interleaved reasoning 内容回传。

    小米 MiMo、部分 Kimi/GLM 等兼容端点会把思考内容放在响应顶层
    `reasoning_content` 字段；如果下一轮请求没有把它随历史 assistant
    消息带回，工具调用后续请求会被服务端以 400 拒绝。

    这里不按 provider 白名单判断，而是只在历史 AIMessage 真实保存过
    `reasoning_content` 时回传，避免以后每接入一个同类模型都要单独适配。
    """
    try:
        import langchain_openai.chat_models.base as _openai_base
        from langchain_openai import ChatOpenAI
    except Exception as err:
        logger.debug(f"跳过 langchain-openai reasoning_content 修补：{err}")
        return

    if not getattr(_openai_base, "_moviepilot_reasoning_response_patched", False):
        original_convert_dict = getattr(_openai_base, "_convert_dict_to_message", None)
        original_convert_delta = getattr(
            _openai_base, "_convert_delta_to_message_chunk", None
        )

        if callable(original_convert_dict):
            @wraps(original_convert_dict)
            def _patched_convert_dict_to_message(message_dict):
                message = original_convert_dict(message_dict)
                if (
                        isinstance(message, AIMessage)
                        and "reasoning_content" in message_dict
                ):
                    message.additional_kwargs["reasoning_content"] = (
                        message_dict.get("reasoning_content") or ""
                    )
                return message

            _openai_base._convert_dict_to_message = _patched_convert_dict_to_message

        if callable(original_convert_delta):
            @wraps(original_convert_delta)
            def _patched_convert_delta_to_message_chunk(delta, default_class):
                chunk = original_convert_delta(delta, default_class)
                if (
                        isinstance(chunk, AIMessageChunk)
                        and "reasoning_content" in delta
                ):
                    chunk.additional_kwargs["reasoning_content"] = (
                        delta.get("reasoning_content") or ""
                    )
                return chunk

            _openai_base._convert_delta_to_message_chunk = (
                _patched_convert_delta_to_message_chunk
            )

        _openai_base._moviepilot_reasoning_response_patched = True

    if getattr(ChatOpenAI, "_moviepilot_interleaved_reasoning_patched", False):
        return

    original_get_request_payload = getattr(ChatOpenAI, "_get_request_payload", None)
    if not callable(original_get_request_payload):
        logger.warning("langchain-openai 缺少 _get_request_payload，无法修补 reasoning_content")
        return

    @wraps(original_get_request_payload)
    def _patched_get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = original_get_request_payload(self, input_, stop=stop, **kwargs)
        if "messages" not in payload:
            return payload

        messages = self._convert_input(input_).to_messages()
        for index, payload_message in enumerate(payload["messages"]):
            if (
                    payload_message.get("role") != "assistant"
                    or index >= len(messages)
                    or not isinstance(messages[index], AIMessage)
                    or "reasoning_content" in payload_message
            ):
                continue

            reasoning_content = messages[index].additional_kwargs.get(
                "reasoning_content"
            )
            if reasoning_content is not None:
                # 只回传模型真实返回过的思考字段。普通模型没有该字段时，
                # payload 保持原样，不额外塞未知参数。
                payload_message["reasoning_content"] = reasoning_content

        return payload

    ChatOpenAI._get_request_payload = _patched_get_request_payload
    ChatOpenAI._moviepilot_interleaved_reasoning_patched = True
    logger.debug("已修补 langchain-openai interleaved reasoning_content 回传兼容性")


def _patch_openai_responses_instructions_support():
    """
    修补 langchain-openai 在使用 use_responses_api=True 时，
    提取 system 消息为顶层 instructions 字段。
    由于 Codex 等模型 (Responses API) 强依赖 instructions 字段，
    如果没有该字段会报 400 "Instructions are required"。
    """
    try:
        from langchain_openai import ChatOpenAI
    except Exception as err:
        logger.debug(f"跳过 langchain-openai instructions 修补：{err}")
        return

    _patch_openai_interleaved_reasoning_content_support()
    _patch_openai_responses_empty_output_support()

    if getattr(ChatOpenAI, "_moviepilot_responses_instructions_patched", False):
        return

    original_get_request_payload = getattr(ChatOpenAI, "_get_request_payload", None)
    if not callable(original_get_request_payload):
        logger.warning("langchain-openai 缺少 _get_request_payload，无法修补 instructions")
        return

    @wraps(original_get_request_payload)
    def _patched_get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = original_get_request_payload(self, input_, stop=stop, **kwargs)

        base_url = str(getattr(self, "openai_api_base", "") or "").lower()

        # 处理 GitHub Copilot 端点兼容性
        if "githubcopilot.com" in base_url:
            payload.pop("stream_options", None)
            payload.pop("metadata", None)

        # 处理 ChatGPT 官方 Responses API (Codex) 端点兼容性
        is_codex = "chatgpt.com/backend-api/codex" in base_url
        
        if is_codex and (getattr(self, "use_responses_api", False) or "input" in payload):
            instructions = payload.get("instructions", "")
            inputs = payload.get("input", [])
            new_inputs = []

            for msg in inputs:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        if instructions:
                            instructions += "\n\n" + content
                        else:
                            instructions = content
                else:
                    new_inputs.append(msg)

            payload["input"] = new_inputs
            payload["instructions"] = instructions or "You are a helpful assistant."
            payload["store"] = False
            
            # Codex 端点不支持的部分常见补全参数，统一清理避免 400 报错
            unsupported_keys = [
                "presence_penalty", "frequency_penalty", "top_p", "n", "user", 
                "stop", "metadata", "logit_bias", "logprobs", "top_logprobs",
                "stream_options", "temperature"
            ]
            for key in unsupported_keys:
                payload.pop(key, None)

        return payload

    ChatOpenAI._get_request_payload = _patched_get_request_payload
    ChatOpenAI._moviepilot_responses_instructions_patched = True
    logger.debug("已修补 langchain-openai responses API 的 instructions 兼容性")


def _patch_openai_responses_empty_output_support():
    """
    修补 langchain-openai Responses API 流式完成事件 output 为空的兼容性。

    ChatGPT Codex 后端有时会在 `response.completed` chunk 里返回
    `response.output = None`，但前面的 delta chunk 已经包含实际文本。
    langchain-openai 在收尾阶段遍历 output 会抛出 TypeError，这里将缺失
    output 规整为空列表，让收尾 chunk 只承载 usage/metadata。
    """
    try:
        import langchain_openai.chat_models.base as _openai_base
    except Exception as err:
        logger.debug(f"跳过 langchain-openai responses output 修补：{err}")
        return

    if getattr(_openai_base, "_moviepilot_responses_empty_output_patched", False):
        return

    original_construct = getattr(
        _openai_base, "_construct_lc_result_from_responses_api", None
    )
    if not callable(original_construct):
        logger.warning("langchain-openai 缺少 Responses API 结果构造函数，无法修补 output")
        return

    def _clone_response_with_empty_output(response):
        """
        复制 Responses 对象，把缺失 output 规整为空列表。
        """
        model_copy = getattr(response, "model_copy", None)
        if callable(model_copy):
            try:
                return model_copy(update={"output": []})
            except Exception as e:
                logger.debug(f"复制 Responses 对象失败，回退原地修补 output：{e}")

        try:
            setattr(response, "output", [])
        except Exception as e:
            logger.debug(f"原地修补 Responses output 失败：{e}")
        return response

    @wraps(original_construct)
    def _patched_construct_lc_result_from_responses_api(response, *args, **kwargs):
        """
        在 Responses API 收尾 chunk 缺少 output 时跳过空内容遍历。
        """
        if hasattr(response, "output") and getattr(response, "output", None) is None:
            response = _clone_response_with_empty_output(response)
        return original_construct(response, *args, **kwargs)

    _openai_base._construct_lc_result_from_responses_api = (
        _patched_construct_lc_result_from_responses_api
    )
    _openai_base._moviepilot_responses_empty_output_patched = True
    logger.debug("已修补 langchain-openai responses API 空 output 兼容性")


class LLMHelper:
    """LLM模型相关辅助功能"""

    _SUPPORTED_THINKING_LEVELS = frozenset(
        {"off", "auto", "minimal", "low", "medium", "high", "max", "xhigh"}
    )

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str:
        """
        统一清理模型名称，便于按模型族做能力映射。
        """
        return (model_name or "").strip().lower()

    @classmethod
    def _normalize_deepseek_reasoning_effort(
            cls, thinking_level: str | None = None
    ) -> str | None:
        """
        DeepSeek 文档当前建议使用 high/max；兼容常见 effort 别名。
        """
        if not thinking_level or thinking_level in {"off", "auto"}:
            return None

        if thinking_level in {"minimal", "low", "medium", "high"}:
            return "high"
        if thinking_level in {"max", "xhigh"}:
            return "max"

        logger.warning(f"忽略不支持的 DeepSeek reasoning_effort 配置: {thinking_level}")
        return None

    @classmethod
    def _normalize_openai_reasoning_effort(
            cls, thinking_level: str | None = None
    ) -> str | None:
        """
        OpenAI reasoning_effort 支持更细粒度的 effort，统一做最近似映射。
        """
        if not thinking_level or thinking_level == "auto":
            return None
        if thinking_level == "off":
            return "none"
        if thinking_level == "max":
            return "xhigh"
        return thinking_level

    @classmethod
    def _build_google_thinking_kwargs(
            cls, model_name: str, thinking_level: str
    ) -> dict[str, Any]:
        """
        Gemini 3 使用 thinking_level；Gemini 2.5 使用 thinking_budget。
        """
        if not model_name or thinking_level == "auto":
            return {}

        if "gemini-2.5" in model_name:
            if thinking_level == "off":
                if "pro" in model_name:
                    # Gemini 2.5 Pro 官方不支持完全关闭思考，回退到最小预算。
                    return {
                        "thinking_budget": 128,
                        "include_thoughts": False,
                    }
                return {
                    "thinking_budget": 0,
                    "include_thoughts": False,
                }

            budget_map = {
                "minimal": 512,
                "low": 1024,
                "medium": 4096,
                "high": 8192,
                "max": 24576,
                "xhigh": 24576,
            }
            budget = budget_map.get(thinking_level)
            return (
                {
                    "thinking_budget": budget,
                    "include_thoughts": False,
                }
                if budget is not None
                else {}
            )

        if "gemini-3" in model_name:
            level_map = {
                "off": "minimal",
                "minimal": "minimal",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "max": "high",
                "xhigh": "high",
            }
            google_level = level_map.get(thinking_level)
            return (
                {
                    "thinking_level": google_level,
                    "include_thoughts": False,
                }
                if google_level
                else {}
            )

        return {}

    @classmethod
    def _build_kimi_thinking_kwargs(
            cls, model_name: str, thinking_level: str
    ) -> dict[str, Any]:
        """
        Kimi 当前公开文档仅支持思考开关，不支持显式深度调节。
        """
        if model_name.startswith("kimi-k2-thinking"):
            return {}
        if thinking_level == "off":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}

    @classmethod
    def _build_thinking_kwargs(
            cls,
            provider: str,
            model: str | None,
            thinking_level: str | None = None
    ) -> dict[str, Any]:
        """
        按 provider/model 生成思考模式相关参数。

        优先使用 LangChain/OpenAI SDK 已支持的原生字段；仅在 provider
        明确要求自定义请求体时，才回退到 extra_body。
        """
        provider_name = (provider or "").strip().lower()
        model_name = cls._normalize_model_name(model)

        if provider_name == "deepseek":
            if thinking_level == "off":
                return {"extra_body": {"thinking": {"type": "disabled"}}}
            if thinking_level == "auto":
                return {}

            kwargs: dict[str, Any] = {"extra_body": {"thinking": {"type": "enabled"}}}
            deepseek_effort = cls._normalize_deepseek_reasoning_effort(
                thinking_level
            )
            if deepseek_effort:
                kwargs["reasoning_effort"] = deepseek_effort
            return kwargs

        if model_name.startswith(("kimi-k2.5", "kimi-k2.6", "kimi-k2-thinking")):
            return cls._build_kimi_thinking_kwargs(model_name, thinking_level)

        if not model_name:
            return {}

        # OpenAI 原生推理模型优先走 LangChain 内置 reasoning_effort。
        if provider_name in {"openai", "chatgpt"} and model_name.startswith(
                ("gpt-5", "o1", "o3", "o4")
        ):
            openai_effort = cls._normalize_openai_reasoning_effort(
                thinking_level
            )
            return {"reasoning_effort": openai_effort} if openai_effort else {}

        # Gemini 使用 google-genai / langchain-google-genai 内置思考控制参数。
        if provider_name == "google":
            return cls._build_google_thinking_kwargs(
                model_name, thinking_level
            )

        return {}

    @staticmethod
    def _metadata_supports_image_input(metadata: Any) -> Optional[bool]:
        """从模型元数据中读取图片输入能力，未知时返回 None。"""
        if not isinstance(metadata, dict):
            return None

        modalities = metadata.get("modalities") or {}
        input_modalities = modalities.get("input")
        if isinstance(input_modalities, str):
            input_modalities = [input_modalities]
        if isinstance(input_modalities, list):
            normalized_modalities = {
                str(item or "").strip().lower() for item in input_modalities
            }
            return "image" in normalized_modalities
        return None

    @classmethod
    def _resolve_catalog_image_input_support(
            cls,
            provider: Optional[str] = None,
            model: Optional[str] = None,
            base_url: Optional[str] = None,
            base_url_preset: Optional[str] = None,
    ) -> Optional[bool]:
        """复用 provider 目录缓存解析当前模型是否支持图片输入。"""
        provider_name = str(provider if provider is not None else settings.LLM_PROVIDER).strip()
        model_name = str(model if model is not None else settings.LLM_MODEL).strip()
        if not provider_name or not model_name:
            return None

        try:
            from app.agent.llm.provider import LLMProviderManager

            metadata = LLMProviderManager().resolve_cached_model_metadata(
                provider_id=provider_name,
                model_id=model_name,
                base_url=base_url if base_url is not None else settings.LLM_BASE_URL,
                base_url_preset_id=(
                    base_url_preset
                    if base_url_preset is not None
                    else settings.LLM_BASE_URL_PRESET
                ),
            )
        except Exception as err:
            logger.debug(f"解析模型图片能力失败: {err}")
            return None

        return cls._metadata_supports_image_input(metadata)

    @classmethod
    def supports_image_input(
            cls,
            provider: Optional[str] = None,
            model: Optional[str] = None,
            base_url: Optional[str] = None,
            base_url_preset: Optional[str] = None,
    ) -> bool:
        """
        判断当前模型是否启用了图片输入能力。

        用户开关为总开关；当内置模型目录明确标注当前模型不支持 image 输入时，
        即使总开关开启也降级为纯文本，避免文本模型收到 `image_url` 内容块后
        被兼容端点以 400 拒绝。无参调用保持旧版“只读总开关”语义，
        未知自定义模型也保持原有开关语义。
        """
        if not settings.LLM_SUPPORT_IMAGE_INPUT:
            return False
        if provider is None and model is None:
            return True

        image_support = cls._resolve_catalog_image_input_support(
            provider=provider,
            model=model,
            base_url=base_url,
            base_url_preset=base_url_preset,
        )
        if image_support is not None:
            return image_support
        return True

    @staticmethod
    def _build_legacy_runtime(
            provider_name: str,
            model_name: str | None,
            api_key: str | None = None,
            base_url: str | None = None,
            user_agent: str | None = None,
    ) -> dict[str, Any]:
        """
        在 provider 目录不可用时回退到旧的直接构造逻辑。

        这主要用于单测 stub 环境以及极端的最小运行环境，正常生产路径仍优先
        走 `LLMProviderManager.resolve_runtime()`。
        """
        api_key_value = api_key if api_key is not None else settings.LLM_API_KEY
        base_url_value = base_url if base_url is not None else settings.LLM_BASE_URL
        if not api_key_value:
            raise ValueError("未配置LLM API Key")

        runtime_name = (
            provider_name
            if provider_name in {"google", "deepseek"}
            else "openai_compatible"
        )
        return {
            "provider_id": provider_name,
            "runtime": runtime_name,
            "model_id": model_name,
            "api_key": api_key_value,
            "base_url": base_url_value,
            "default_headers": LLMHelper._build_openai_default_headers(
                None,
                user_agent=user_agent,
            ),
            "use_responses_api": None,
            "model_record": None,
            "model_metadata": None,
        }

    @staticmethod
    def _build_openai_default_headers(
            default_headers: dict[str, str] | None = None,
            user_agent: str | None = None,
    ) -> dict[str, str] | None:
        """
        合并 OpenAI 兼容接口默认请求头。

        :param default_headers: provider 运行时已解析的默认请求头
        :param user_agent: 用户配置的 User-Agent，非空时写入标准请求头
        :return: 可传给 OpenAI SDK 的请求头字典
        """
        headers = dict(default_headers or {})
        normalized_user_agent = str(user_agent or "").strip()
        if normalized_user_agent:
            for key in list(headers.keys()):
                if key.lower() == "user-agent":
                    headers.pop(key)
            headers["User-Agent"] = normalized_user_agent
        return headers or None

    @classmethod
    def _should_use_openai_responses_api(
            cls,
            provider: str,
            model: str | None,
            runtime: dict[str, Any],
    ) -> bool | None:
        """
        判断官方 ChatGPT API Key 模式是否应使用 Responses API。

        GPT-5/o 系推理模型在 Chat Completions 中组合 function tools 与
        reasoning_effort 时会被官方端点拒绝，因此 ChatGPT 官方 API Key
        模式需要显式切到 Responses API；通用 OpenAI-compatible 入口保持
        provider 目录解析出的默认行为，避免误伤第三方兼容服务。
        """
        runtime_use_responses_api = runtime.get("use_responses_api")
        if runtime_use_responses_api is not None:
            return bool(runtime_use_responses_api)

        provider_name = (provider or "").strip().lower()
        if provider_name != "chatgpt":
            return None

        base_url = str(runtime.get("base_url") or "").strip().lower()
        if "api.openai.com" not in base_url:
            return None

        model_name = cls._normalize_model_name(model)
        if model_name.startswith(("gpt-5", "o1", "o3", "o4")):
            return True
        return None

    @staticmethod
    def _attach_runtime_metadata(model: Any, runtime: dict[str, Any]) -> None:
        """
        将 MoviePilot 已解析出的 provider 运行时信息挂到模型实例上。

        这些字段只供内部中间件识别协议能力，不参与 LangChain 请求序列化。
        """
        runtime_metadata = {
            "runtime": runtime.get("runtime"),
            "provider_id": runtime.get("provider_id"),
            "base_url": runtime.get("base_url"),
        }

        def _set_metadata_attr(name: str, value: Any) -> None:
            try:
                setattr(model, name, value)
            except Exception:
                object.__setattr__(model, name, value)

        try:
            _set_metadata_attr("_moviepilot_llm_runtime", runtime_metadata["runtime"])
            _set_metadata_attr(
                "_moviepilot_llm_provider_id",
                runtime_metadata["provider_id"],
            )
            _set_metadata_attr("_moviepilot_llm_base_url", runtime_metadata["base_url"])
        except Exception as err:
            logger.debug(f"LLM运行时元数据附加失败: {str(err)}")

        profile = getattr(model, "profile", None)
        if isinstance(profile, dict):
            profile["moviepilot_runtime"] = runtime_metadata["runtime"]
            profile["moviepilot_provider_id"] = runtime_metadata["provider_id"]
            profile["moviepilot_base_url"] = runtime_metadata["base_url"]

    @classmethod
    def _resolve_thinking_level(
            cls,
            thinking_level: str | None = None,
    ) -> str | None:
        """
        统一兼容新旧 thinking 参数。
        """

        def _normalize(value: str | None) -> str | None:
            normalized = str(value or "").strip().lower()
            if not normalized:
                return None
            alias_map = {
                "none": "off",
                "disabled": "off",
                "disable": "off",
                "enabled": "auto",
                "enable": "auto",
                "default": "auto",
                "dynamic": "auto",
            }
            normalized = alias_map.get(normalized, normalized)
            if normalized in cls._SUPPORTED_THINKING_LEVELS:
                return normalized
            logger.warning(f"忽略不支持的思考级别: {value}")
            return None

        normalized_thinking_level = _normalize(thinking_level)
        if normalized_thinking_level:
            return normalized_thinking_level

        return "off"

    @classmethod
    async def get_llm(
            cls,
            streaming: bool = False,
            provider: str | None = None,
            model: str | None = None,
            thinking_level: str | None = None,
            api_key: str | None = None,
            base_url: str | None = None,
            base_url_preset: str | None = None,
            user_agent: str | None = None,
            temperature: Optional[float] = None,
            use_proxy: bool | None = None,
    ):
        """
        获取LLM实例
        :param streaming: 是否启用流式输出
        :param provider: LLM提供商，默认为配置项LLM_PROVIDER
        :param model: 模型名称，默认为配置项LLM_MODEL
        :param thinking_level: 思考模式级别，默认为 None（即自动判断
            是否启用思考模式）。支持的级别包括 "off"（关闭）、"auto"（自动）、"minimal"、"low"、"medium"、"high"、"max"/"xhigh"（最大）。
            不同模型对思考模式的支持和表现不同，具体映射关系请
            参考代码实现。对于不支持思考模式的模型，该参数将被忽略。
        :param api_key: API Key。未显式传入时使用当前配置项 LLM_API_KEY。对于某些提供商（如 DeepSeek），可能需要同时提供 base_url。
        :param base_url: API Base URL。未显式传入时使用当前配置项 LLM_BASE_URL。
        :param base_url_preset: Base URL 预设。未显式传入时使用当前配置项 LLM_BASE_URL_PRESET。
        :param user_agent: OpenAI兼容接口请求 User-Agent。未显式传入时使用配置项 LLM_USER_AGENT。
        :param temperature: LLM 温度参数。未显式传入时使用配置项 LLM_TEMPERATURE。
        :param use_proxy: 是否为本次 LLM 调用使用系统代理。未显式传入时使用配置项 LLM_USE_PROXY。
        :return: LLM实例
        """
        provider_name = str(provider if provider is not None else settings.LLM_PROVIDER).lower()
        model_name = model if model is not None else settings.LLM_MODEL
        api_key_value = api_key if api_key is not None else settings.LLM_API_KEY
        base_url_value = base_url if base_url is not None else settings.LLM_BASE_URL
        base_url_preset_value = (
            base_url_preset if base_url_preset is not None else settings.LLM_BASE_URL_PRESET
        )
        user_agent_value = user_agent if user_agent is not None else settings.LLM_USER_AGENT
        temperature_value = temperature if temperature is not None else settings.LLM_TEMPERATURE
        normalized_thinking_level = cls._resolve_thinking_level(
            thinking_level=thinking_level,
        )
        try:
            # 延迟导入，避免单测在最小 stub 环境下 import `llm.py` 时被 provider
            # 目录依赖链拖住。
            from app.agent.llm.provider import LLMProviderManager

            runtime = await LLMProviderManager().resolve_runtime(
                provider_id=provider_name,
                model=model_name,
                api_key=api_key_value,
                base_url=base_url_value,
                base_url_preset_id=base_url_preset_value,
                user_agent=user_agent_value,
                use_proxy=use_proxy,
            )
        except Exception as err:
            logger.debug(f"LLM provider 目录不可用，回退到旧运行时逻辑: {err}")
            runtime = cls._build_legacy_runtime(
                provider_name=provider_name,
                model_name=model_name,
                api_key=api_key_value,
                base_url=base_url_value,
                user_agent=user_agent_value,
            )
        model_name = runtime.get("model_id") or model_name
        default_headers = cls._build_openai_default_headers(
            runtime.get("default_headers"),
            user_agent=user_agent_value,
        )
        thinking_kwargs = cls._build_thinking_kwargs(
            provider=provider_name,
            model=model_name,
            thinking_level=normalized_thinking_level,
        )
        use_responses_api = cls._should_use_openai_responses_api(
            provider=provider_name,
            model=model_name,
            runtime=runtime,
        )
        llm_proxy = _resolve_llm_proxy(use_proxy)

        if runtime["runtime"] == "google":
            # 修补 Gemini 2.5 思考模型的 thought_signature 兼容性
            _patch_gemini_thought_signature()

            # 统一使用 langchain-google-genai 原生接口
            # 不使用 OpenAI 兼容端点，因其不支持 Gemini 思考模型的 thought_signature，
            # 会导致工具调用时报错 400
            from langchain_google_genai import ChatGoogleGenerativeAI

            model = ChatGoogleGenerativeAI(
                model=model_name,
                api_key=runtime["api_key"],
                retries=3,
                temperature=temperature_value,
                streaming=streaming,
                client_args=_build_google_client_args(llm_proxy),
                **thinking_kwargs,
            )
        elif runtime["runtime"] == "deepseek":
            from langchain_deepseek import ChatDeepSeek

            _patch_deepseek_reasoning_content_support()
            model = ChatDeepSeek(
                model=model_name,
                api_key=runtime["api_key"],
                api_base=runtime["base_url"],
                max_retries=3,
                temperature=temperature_value,
                streaming=streaming,
                stream_usage=True,
                http_client=_build_httpx_client(llm_proxy),
                http_async_client=_build_httpx_client(llm_proxy, async_client=True),
                **thinking_kwargs,
            )
        elif runtime["runtime"] in {"anthropic_compatible", "copilot_anthropic"}:
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model=model_name,
                api_key=runtime["api_key"],
                base_url=runtime["base_url"],
                max_retries=3,
                temperature=temperature_value,
                streaming=streaming,
                stream_usage=True,
                anthropic_proxy=llm_proxy,
                default_headers=default_headers,
                **thinking_kwargs,
            )
        else:
            from langchain_openai import ChatOpenAI

            _patch_openai_responses_instructions_support()
            
            # ChatGPT Codex 端点强制要求 stream: True
            if runtime.get("use_responses_api") and "chatgpt.com/backend-api/codex" in str(runtime.get("base_url") or ""):
                streaming = True

            model = ChatOpenAI(
                model=model_name,
                api_key=runtime["api_key"],
                max_retries=3,
                base_url=runtime.get("base_url"),
                temperature=temperature_value,
                streaming=streaming,
                stream_usage=True,
                openai_proxy=llm_proxy,
                **(
                    {}
                    if llm_proxy
                    else {
                        "http_client": _build_httpx_client(llm_proxy),
                        "http_async_client": _build_httpx_client(llm_proxy, async_client=True),
                    }
                ),
                default_headers=default_headers,
                use_responses_api=use_responses_api,
                **thinking_kwargs,
            )

        # 优先使用 provider / models.dev 目录中的上下文上限，减少用户手填成本。
        model_profile = getattr(model, "profile", None)
        if model_profile:
            logger.debug(f"使用LLM模型: {model.model}，Profile: {model.profile}")
        else:
            model_record = runtime.get("model_record") or {}
            model_metadata = runtime.get("model_metadata") or {}
            metadata_limit = model_metadata.get("limit") or {}
            max_input_tokens = (
                    model_record.get("input_tokens")
                    or model_record.get("context_tokens")
                    or metadata_limit.get("input")
                    or metadata_limit.get("context")
                    or settings.LLM_MAX_CONTEXT_TOKENS * 1000
            )
            model.profile = {
                "max_input_tokens": int(max_input_tokens),
            }

        cls._attach_runtime_metadata(model, runtime)
        return model

    @staticmethod
    def extract_text_content(content: Any, fallback_to_string: bool = False) -> str:
        """
        从响应内容中提取纯文本，仅保留真实文本块。

        :param content: 模型响应内容，可能是字符串、字典或内容块列表
        :param fallback_to_string: 未识别为文本内容时是否回退为字符串
        :return: 提取后的纯文本内容
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                    continue

                if isinstance(block, dict) or hasattr(block, "get"):
                    block_type = block.get("type")
                    if block.get("thought") or block_type in (
                            "thinking",
                            "reasoning_content",
                            "reasoning",
                            "thought",
                    ):
                        continue
                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                        continue
                    if not block_type and isinstance(block.get("text"), str):
                        text_parts.append(block.get("text", ""))
            return "".join(text_parts)
        if isinstance(content, dict) or hasattr(content, "get"):
            if content.get("thought"):
                return ""
            if content.get("type") == "text":
                return content.get("text", "")
            if not content.get("type") and isinstance(content.get("text"), str):
                return content.get("text", "")
        return str(content) if fallback_to_string else ""

    @staticmethod
    async def test_current_settings(
            prompt: str = "请只回复 OK",
            timeout: int = 20,
            provider: str | None = None,
            model: str | None = None,
            thinking_level: str | None = None,
            api_key: str | None = None,
            base_url: str | None = None,
            base_url_preset: str | None = None,
            user_agent: str | None = None,
            temperature: Optional[float] = None,
            use_proxy: bool | None = None,
    ) -> dict:
        """
        使用当前配置或显式传入的临时配置执行一次最小 LLM 调用。

        :param temperature: LLM 温度参数。未显式传入时沿用已保存配置。
        """
        provider_name = provider if provider is not None else settings.LLM_PROVIDER
        model_name = model if model is not None else settings.LLM_MODEL
        start = time.perf_counter()
        llm_kwargs = {
            "streaming": False,
            "provider": provider_name,
            "model": model_name,
            "thinking_level": thinking_level,
            "api_key": api_key,
            "base_url": base_url,
            "base_url_preset": base_url_preset,
            "user_agent": user_agent,
            "use_proxy": use_proxy,
        }
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        llm = await LLMHelper.get_llm(**llm_kwargs)
        try:
            response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=timeout)
        except TimeoutError as err:
            duration_ms = round((time.perf_counter() - start) * 1000)
            raise LLMTestTimeout("LLM 调用超时", duration_ms=duration_ms) from err
        except Exception as err:
            duration_ms = round((time.perf_counter() - start) * 1000)
            raise LLMTestError(str(err), duration_ms=duration_ms) from err

        reply_text = LLMHelper.extract_text_content(
            getattr(response, "content", response)
        ).strip()
        duration_ms = round((time.perf_counter() - start) * 1000)

        data = {
            "provider": provider_name,
            "model": model_name,
            "duration_ms": duration_ms,
        }
        if reply_text:
            data["reply_preview"] = reply_text[:120]
        return data

    async def get_models(
            self,
            provider: str,
            api_key: str | None = None,
            base_url: str | None = None,
            base_url_preset: str | None = None,
            user_agent: str | None = None,
            use_proxy: bool | None = None,
            force_refresh: bool = False,
    ) -> List[dict[str, Any]]:
        """
        获取模型列表。

        返回值会带上 context/supports_reasoning 等元数据，供前端直接渲染并自动
        回填上下文大小。
        """
        logger.info(f"获取 {provider} 模型列表...")
        try:
            from app.agent.llm.provider import LLMProviderManager

            return await LLMProviderManager().list_models(
                provider_id=provider,
                api_key=api_key,
                base_url=base_url,
                base_url_preset_id=base_url_preset,
                user_agent=user_agent,
                use_proxy=use_proxy,
                force_refresh=force_refresh,
            )
        except Exception as err:
            logger.debug(f"LLM provider 目录不可用，回退旧模型列表逻辑: {err}")
            if provider == "google":
                return [
                    {"id": model_id, "name": model_id}
                    for model_id in await self._get_google_models(
                        api_key or "",
                        use_proxy=use_proxy,
                    )
                ]
            try:
                from app.agent.llm.provider import LLMProviderManager

                model_list_base_url = (
                    LLMProviderManager().resolve_model_list_base_url(
                        provider_id=provider,
                        base_url=base_url,
                        base_url_preset_id=base_url_preset,
                    )
                    or base_url
                )
            except Exception:
                model_list_base_url = base_url
            return [
                {"id": model_id, "name": model_id}
                for model_id in await self._get_openai_compatible_models(
                    provider,
                    api_key or "",
                    model_list_base_url,
                    user_agent=user_agent,
                    use_proxy=use_proxy,
                )
            ]

    @staticmethod
    async def _get_google_models(api_key: str, use_proxy: bool | None = None) -> List[str]:
        """获取Google模型列表（使用 google-genai SDK v1）"""
        try:
            from google import genai
            from google.genai.types import HttpOptions

            llm_proxy = _resolve_llm_proxy(use_proxy)
            google_client_args = _build_google_client_args(llm_proxy)
            http_options = HttpOptions(
                client_args=google_client_args,
                async_client_args=google_client_args,
            )

            client = genai.Client(api_key=api_key, http_options=http_options)
            models = await client.aio.models.list()
            result = [
                m.name
                for m in models.page
                if m.supported_actions and "generateContent" in m.supported_actions
            ]
            await client.aio.aclose()
            return result
        except Exception as e:
            logger.error(f"获取Google模型列表失败：{e}")
            raise e

    @staticmethod
    async def _get_openai_compatible_models(
            provider: str,
            api_key: str,
            base_url: str = None,
            user_agent: str | None = None,
            use_proxy: bool | None = None,
    ) -> List[str]:
        """获取OpenAI兼容模型列表"""
        try:
            from openai import AsyncOpenAI

            if provider == "deepseek":
                base_url = base_url or "https://api.deepseek.com"

            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=LLMHelper._build_openai_default_headers(
                    None,
                    user_agent=user_agent,
                ),
                http_client=_build_httpx_client(
                    _resolve_llm_proxy(use_proxy),
                    async_client=True,
                    timeout=15.0,
                ),
            )
            models = await client.models.list()
            await client.close()
            return [model.id for model in models.data]
        except Exception as e:
            logger.error(f"获取 {provider} 模型列表失败：{e}")
            raise e
