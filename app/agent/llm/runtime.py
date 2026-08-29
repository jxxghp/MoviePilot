"""LLM Provider 运行时参数解析的唯一 owner。"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.log import logger


class LLMProviderError(RuntimeError):
    """通用 LLM provider 异常。"""


class LLMProviderAuthError(LLMProviderError):
    """LLM provider 鉴权异常。"""


class _ProviderRuntime:
    """LLM Provider 运行时参数解析的唯一 owner。"""

    def __getattr__(self, name: str) -> Any:
        """将跨 owner 调用交给最终 Facade 的 MRO 解析。"""
        raise AttributeError(name)

    async def resolve_runtime(
        self,
        provider_id: str,
        model: Optional[str],
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any]:
        """
        解析 provider 运行时参数。

        返回统一结构，供 `LLMHelper` 创建具体 LangChain 模型实例时使用。
        """
        normalized_provider_id = self._normalize_provider_id(provider_id)
        normalized_base_url_preset_id = self._normalize_base_url_preset_id(
            normalized_provider_id,
            base_url_preset_id,
        )
        spec = await self._get_provider_async(
            normalized_provider_id,
            use_proxy=use_proxy,
        )
        resolved_runtime = self._resolve_provider_runtime(
            spec,
            base_url,
            base_url_preset_id=normalized_base_url_preset_id,
        )
        normalized_api_key = str(api_key or "").strip() or None
        normalized_base_url = self._sanitize_base_url(base_url)
        default_transport = "anthropic" if resolved_runtime == "anthropic_compatible" else "openai"
        model_record = self._resolve_cached_model_record(
            normalized_provider_id,
            model,
            base_url=base_url,
            base_url_preset_id=normalized_base_url_preset_id,
            transport=default_transport,
        )
        model_metadata = self.resolve_cached_model_metadata(
            normalized_provider_id,
            model,
            base_url=base_url,
            base_url_preset_id=normalized_base_url_preset_id,
        )

        result: dict[str, Any] = {
            "provider_id": normalized_provider_id,
            "runtime": resolved_runtime,
            "model_id": model,
            "model_record": model_record,
            "model_metadata": model_metadata,
            "model_profile_endpoint_matched": (
                self._is_model_profile_endpoint_matched(
                    spec,
                    base_url,
                    base_url_preset_id=normalized_base_url_preset_id,
                )
            ),
            "supports_prompt_cache": self._metadata_supports_prompt_cache(model_metadata),
            "default_headers": None,
            "use_responses_api": None,
            "auth_mode": "api_key",
        }

        if normalized_provider_id == "chatgpt":
            auth = None
            try:
                auth = await self._resolve_chatgpt_oauth()
            except Exception as err:
                logger.debug(f"解析 ChatGPT OAuth 鉴权失败，回退 API Key 模式: {err}")

            if auth:
                headers = {"originator": "moviepilot"}
                if auth.get("account_id"):
                    headers["ChatGPT-Account-Id"] = auth["account_id"]
                result.update(
                    {
                        "runtime": "chatgpt",
                        "api_key": auth["access_token"],
                        "base_url": self._CHATGPT_CODEX_BASE_URL,
                        "default_headers": self._merge_user_agent_header(
                            headers,
                            user_agent,
                        ),
                        "use_responses_api": True,
                        "auth_mode": "oauth",
                    }
                )
                return result

            if normalized_api_key:
                result.update(
                    {
                        "runtime": "openai_compatible",
                        "api_key": normalized_api_key,
                        "base_url": normalized_base_url or self._default_base_url_for_provider(spec),
                        "default_headers": self._merge_user_agent_header(
                            None,
                            user_agent,
                        ),
                        "auth_mode": "api_key",
                    }
                )
                return result

            raise LLMProviderAuthError("请提供 API Key 或完成 ChatGPT 授权")

        if normalized_provider_id == "github-copilot":
            auth = self.get_saved_auth("github-copilot")
            if auth and auth.get("type") == "oauth":
                token = auth.get("refresh_token") or auth.get("access_token")
            elif normalized_api_key:
                token = normalized_api_key
            else:
                raise LLMProviderAuthError("请先完成 GitHub Copilot 授权")

            transport = (model_record or {}).get("transport") or "openai"
            result.update(
                {
                    "runtime": "copilot_anthropic" if transport == "anthropic" else "github_copilot",
                    "api_key": token,
                    "base_url": "https://api.githubcopilot.com",
                    "default_headers": self._merge_user_agent_header(
                        self._copilot_headers(
                            token,
                            include_auth=transport == "anthropic",
                        ),
                        user_agent,
                    ),
                    "auth_mode": "oauth" if auth else "api_key",
                }
            )
            return result

        if resolved_runtime == "google":
            if not normalized_api_key:
                raise LLMProviderAuthError(f"{spec.name} 需要填写 API Key")
            result.update(
                {
                    "api_key": normalized_api_key,
                    "base_url": None,
                    "auth_mode": "api_key",
                }
            )
            return result

        if resolved_runtime == "bedrock":
            effective_base_url = normalized_base_url or self._default_base_url_for_provider(spec)
            credentials = self._parse_bedrock_credentials(normalized_api_key)
            result.update(
                {
                    "api_key": normalized_api_key,
                    "base_url": effective_base_url,
                    "aws_region": self._extract_bedrock_region(effective_base_url),
                    "aws_auth": credentials,
                    "auth_mode": "api_key",
                }
            )
            return result

        if resolved_runtime == "anthropic_compatible":
            effective_base_url = normalized_base_url or self._default_base_url_for_provider(spec)
            if not normalized_api_key:
                raise LLMProviderAuthError(f"{spec.name} 需要填写 API Key")
            if not effective_base_url:
                raise LLMProviderAuthError(f"{spec.name} 缺少 Base URL")
            result.update(
                {
                    "api_key": normalized_api_key,
                    "base_url": self._normalize_base_url_for_anthropic(effective_base_url),
                    "default_headers": self._merge_user_agent_header(
                        None,
                        user_agent,
                    ),
                    "auth_mode": "api_key",
                }
            )
            return result

        effective_base_url = normalized_base_url or self._default_base_url_for_provider(spec)
        if spec.requires_base_url and not effective_base_url:
            raise LLMProviderAuthError(f"{spec.name} 需要填写 Base URL")
        if not normalized_api_key:
            raise LLMProviderAuthError(f"{spec.name} 需要填写 API Key")
        result.update(
            {
                "api_key": normalized_api_key,
                "base_url": effective_base_url,
                "default_headers": self._merge_user_agent_header(None, user_agent),
                "auth_mode": "api_key",
            }
        )
        return result
