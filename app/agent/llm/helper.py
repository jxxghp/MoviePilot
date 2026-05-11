"""LLM模型相关辅助功能"""

import asyncio
import inspect
import json
import time
from functools import wraps
from typing import Any, List

from langchain_core.messages import AIMessage

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
    langchain-google-genai 的 _is_gemini_3_or_later() 仅检查 "gemini-3"，
    导致 Gemini 2.5 思考模型（如 gemini-2.5-flash、gemini-2.5-pro）在工具调用时
    缺少 thought_signature 而报错 400。
    此补丁将检查范围扩展到 Gemini 2.5 模型。
    """
    try:
        import langchain_google_genai.chat_models as _cm

        # 仅在未修补时执行
        if getattr(_cm, "_thought_signature_patched", False):
            return

        def _patched_is_gemini_3_or_later(model_name: str) -> bool:
            if not model_name:
                return False
            name = model_name.lower().replace("models/", "")
            # Gemini 2.5 思考模型也需要 thought_signature 支持
            return "gemini-3" in name or "gemini-2.5" in name

        _cm._is_gemini_3_or_later = _patched_is_gemini_3_or_later
        _cm._thought_signature_patched = True
        logger.debug(
            "已修补 langchain-google-genai thought_signature 兼容性（覆盖 Gemini 2.5 模型）"
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

        # Resolve original messages so we can extract reasoning_content from
        # additional_kwargs.  The parent's payload builder does not propagate
        # this DeepSeek-specific field.
        messages = self._convert_input(input_).to_messages()

        for i, message in enumerate(payload["messages"]):
            if message["role"] == "tool" and isinstance(message["content"], list):
                message["content"] = json.dumps(message["content"])
            elif message["role"] == "assistant":
                if isinstance(message["content"], list):
                    # DeepSeek API expects assistant content to be a string,
                    # not a list. Extract text blocks and join them, or use
                    # empty string if none exist.
                    text_parts = [
                        block.get("text", "")
                        for block in message["content"]
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    message["content"] = "".join(text_parts) if text_parts else ""

                # DeepSeek reasoning models require every assistant message to
                # carry a reasoning_content field (even when empty).  The value
                # is stored in AIMessage.additional_kwargs by
                # _create_chat_result(); re-inject it into the API payload.
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
    def supports_image_input() -> bool:
        """
        判断当前模型是否启用了图片输入能力。
        """
        return bool(settings.LLM_SUPPORT_IMAGE_INPUT)

    @staticmethod
    def _build_legacy_runtime(
            provider_name: str,
            model_name: str | None,
            api_key: str | None = None,
            base_url: str | None = None,
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
            "default_headers": None,
            "use_responses_api": None,
            "model_record": None,
            "model_metadata": None,
        }

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
            api_key: str | None = settings.LLM_API_KEY,
            base_url: str | None = settings.LLM_BASE_URL,
            base_url_preset: str | None = settings.LLM_BASE_URL_PRESET,
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
        :param api_key: API Key，默认为配置项LLM_API_KEY。对于某些提供商（如 DeepSeek），可能需要同时提供 base_url。
        :param base_url: API Base URL，默认为配置项LLM_BASE_URL。
        :return: LLM实例
        """
        provider_name = str(provider if provider is not None else settings.LLM_PROVIDER).lower()
        model_name = model if model is not None else settings.LLM_MODEL
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
                api_key=api_key,
                base_url=base_url,
                base_url_preset_id=base_url_preset,
            )
        except Exception as err:
            logger.debug(f"LLM provider 目录不可用，回退到旧运行时逻辑: {err}")
            runtime = cls._build_legacy_runtime(
                provider_name=provider_name,
                model_name=model_name,
                api_key=api_key,
                base_url=base_url,
            )
        model_name = runtime.get("model_id") or model_name
        thinking_kwargs = cls._build_thinking_kwargs(
            provider=provider_name,
            model=model_name,
            thinking_level=normalized_thinking_level,
        )

        if runtime["runtime"] == "google":
            # 修补 Gemini 2.5 思考模型的 thought_signature 兼容性
            _patch_gemini_thought_signature()

            # 统一使用 langchain-google-genai 原生接口
            # 不使用 OpenAI 兼容端点，因其不支持 Gemini 思考模型的 thought_signature，
            # 会导致工具调用时报错 400
            from langchain_google_genai import ChatGoogleGenerativeAI

            client_args = None
            if settings.PROXY_HOST:
                proxy_key = _get_httpx_proxy_key()
                client_args = {proxy_key: settings.PROXY_HOST}

            model = ChatGoogleGenerativeAI(
                model=model_name,
                api_key=runtime["api_key"],
                retries=3,
                temperature=settings.LLM_TEMPERATURE,
                streaming=streaming,
                client_args=client_args,
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
                temperature=settings.LLM_TEMPERATURE,
                streaming=streaming,
                stream_usage=True,
                **thinking_kwargs,
            )
        elif runtime["runtime"] in {"anthropic_compatible", "copilot_anthropic"}:
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model=model_name,
                api_key=runtime["api_key"],
                base_url=runtime["base_url"],
                max_retries=3,
                temperature=settings.LLM_TEMPERATURE,
                streaming=streaming,
                stream_usage=True,
                anthropic_proxy=settings.PROXY_HOST,
                default_headers=runtime.get("default_headers"),
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
                temperature=settings.LLM_TEMPERATURE,
                streaming=streaming,
                stream_usage=True,
                openai_proxy=settings.PROXY_HOST,
                default_headers=runtime.get("default_headers"),
                use_responses_api=runtime.get("use_responses_api"),
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

        return model

    @staticmethod
    def _extract_text_content(content) -> str:
        """
        从响应内容中提取纯文本，仅保留真实文本块。
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
        return ""

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
    ) -> dict:
        """
        使用当前已保存配置执行一次最小 LLM 调用。
        """
        provider_name = provider if provider is not None else settings.LLM_PROVIDER
        model_name = model if model is not None else settings.LLM_MODEL
        start = time.perf_counter()
        llm = await LLMHelper.get_llm(
            streaming=False,
            provider=provider_name,
            model=model_name,
            thinking_level=thinking_level,
            api_key=api_key,
            base_url=base_url,
            base_url_preset=base_url_preset,
        )
        try:
            response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=timeout)
        except TimeoutError as err:
            duration_ms = round((time.perf_counter() - start) * 1000)
            raise LLMTestTimeout("LLM 调用超时", duration_ms=duration_ms) from err
        except Exception as err:
            duration_ms = round((time.perf_counter() - start) * 1000)
            raise LLMTestError(str(err), duration_ms=duration_ms) from err

        reply_text = LLMHelper._extract_text_content(
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
                force_refresh=force_refresh,
            )
        except Exception as err:
            logger.debug(f"LLM provider 目录不可用，回退旧模型列表逻辑: {err}")
            if provider == "google":
                return [
                    {"id": model_id, "name": model_id}
                    for model_id in await self._get_google_models(api_key or "")
                ]
            model_list_base_url = base_url
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
                )
            ]

    @staticmethod
    async def _get_google_models(api_key: str) -> List[str]:
        """获取Google模型列表（使用 google-genai SDK v1）"""
        try:
            from google import genai
            from google.genai.types import HttpOptions

            http_options = None
            if settings.PROXY_HOST:
                proxy_key = _get_httpx_proxy_key()
                proxy_args = {proxy_key: settings.PROXY_HOST}
                http_options = HttpOptions(
                    client_args=proxy_args,
                    async_client_args=proxy_args,
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
            provider: str, api_key: str, base_url: str = None
    ) -> List[str]:
        """获取OpenAI兼容模型列表"""
        try:
            from openai import AsyncOpenAI

            if provider == "deepseek":
                base_url = base_url or "https://api.deepseek.com"

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            models = await client.models.list()
            await client.close()
            return [model.id for model in models.data]
        except Exception as e:
            logger.error(f"获取 {provider} 模型列表失败：{e}")
            raise e
