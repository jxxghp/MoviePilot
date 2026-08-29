"""LLM Provider 稳定 Facade。"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from app.agent.llm.auth import _ProviderAuth
from app.agent.llm.auth import render_auth_result_html as _render_auth_result_html
from app.agent.llm.catalog import (
    ProviderAuthMethod,
    ProviderSpec,
    ProviderUrlPreset,
    _ProviderCatalog,
)
from app.agent.llm.discovery import _ProviderDiscovery, attach_server_tool_capabilities
from app.agent.llm.runtime import (
    LLMProviderAuthError,
    LLMProviderError,
    _ProviderRuntime,
)
from app.agent.llm.session import PendingAuthSession, _ProviderSession

__all__ = [
    "LLMProviderAuthError",
    "LLMProviderError",
    "LLMProviderManager",
    "PendingAuthSession",
    "ProviderAuthMethod",
    "ProviderSpec",
    "ProviderUrlPreset",
    "render_auth_result_html",
]
from app.foundation.singleton import Singleton
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import LlmProviderAction


class LLMProviderManager(
    _ProviderCatalog,
    _ProviderDiscovery,
    _ProviderAuth,
    _ProviderSession,
    _ProviderRuntime,
    metaclass=Singleton,
):
    """组合 Provider 各职责 owner，并稳定既有公开调用签名。"""

    def __init__(self):
        """初始化 Provider 目录缓存与临时授权会话状态。"""
        self._lock = threading.RLock()
        self._models_dev_lock = asyncio.Lock()
        self._pending_sessions: dict[str, PendingAuthSession] = {}
        self._oauth_state_index: dict[str, str] = {}
        self._models_dev_data: dict[str, Any] | None = None
        self._models_dev_loaded_at: float = 0
        self._models_dev_cache_path = Path(get_runtime_setting("TEMP_PATH")) / "llm_provider_models_dev_cache.json"

    async def clear_auth(self, provider_id: str) -> None:
        """移除 Provider 持久鉴权信息。"""
        return await _ProviderAuth.clear_auth(self, provider_id)

    def create_bedrock_client(
        self,
        service_name: str,
        region: str,
        credentials: dict[str, Any],
        base_url: Optional[str] = None,
        use_proxy: Optional[bool] = None,
        read_timeout: Optional[int] = None,
    ) -> Any:
        """创建使用统一认证和网络策略的 Bedrock 客户端。"""
        return _ProviderDiscovery.create_bedrock_client(
            self,
            service_name,
            region,
            credentials,
            base_url,
            use_proxy,
            read_timeout,
        )

    def get_auth_status(self, provider_id: str) -> dict[str, Any]:
        """返回前端展示用的 Provider 鉴权摘要。"""
        return _ProviderAuth.get_auth_status(self, provider_id)

    async def get_models_dev_data(
        self,
        force_refresh: bool = False,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any]:
        """返回带本地缓存与离线回退的 models.dev 原始目录。"""
        return await _ProviderDiscovery.get_models_dev_data(
            self,
            force_refresh,
            use_proxy,
        )

    def get_provider(self, provider_id: str) -> ProviderSpec:
        """按标识返回 Provider 定义。"""
        return _ProviderCatalog.get_provider(self, provider_id)

    def get_saved_auth(self, provider_id: str) -> dict[str, Any] | None:
        """读取 Provider 持久鉴权信息。"""
        return _ProviderAuth.get_saved_auth(self, provider_id)

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """返回临时授权会话的公开状态。"""
        return _ProviderSession.get_session_status(self, session_id)

    async def handle_chatgpt_callback(
        self,
        provider_id: str,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str],
        error_description: Optional[str],
    ) -> tuple[bool, str]:
        """完成 ChatGPT OAuth 回调并返回公开结果。"""
        return await _ProviderAuth.handle_chatgpt_callback(
            self,
            provider_id,
            code,
            state,
            error,
            error_description,
        )

    async def list_models(
        self,
        provider_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        use_proxy: Optional[bool] = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """返回 Provider 可用模型目录。"""
        return await _ProviderDiscovery.list_models(
            self,
            provider_id,
            api_key,
            base_url,
            base_url_preset_id,
            user_agent,
            use_proxy,
            force_refresh,
        )

    def list_providers(self) -> list[dict[str, Any]]:
        """同步返回当前缓存可见的 Provider 目录。"""
        return _ProviderCatalog.list_providers(self)

    async def list_providers_async(
        self,
        force_refresh: bool = False,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """刷新动态目录后返回 Provider 目录。"""
        return await _ProviderCatalog.list_providers_async(
            self,
            force_refresh,
            use_proxy,
        )

    async def poll_auth_session(self, session_id: str) -> dict[str, Any]:
        """轮询一次设备码授权会话。"""
        return await _ProviderSession.poll_auth_session(self, session_id)

    async def provider_manage(self, provider: str, action: str, **params: Any) -> Dict[str, Any]:
        """
        LLM 提供商统一管理入口。

        按公共动作词汇表分发，统一返回 {"success", "message", "data"}，
        临时配置默认值填充、密钥脱敏与错误归因改写均封闭在此，
        上层透传时无需感知任何提供商特色。
        """
        normalized = action.value if isinstance(action, LlmProviderAction) else str(action)
        try:
            if normalized == LlmProviderAction.LIST_PROVIDERS.value:
                return {"success": True, "message": "", "data": await self.list_providers_async()}
            if normalized == LlmProviderAction.LIST_MODELS.value:
                return await self._manage_list_models(provider, **params)
            if normalized == LlmProviderAction.START_AUTH.value:
                data = await self.start_auth(provider, str(params.get("method") or ""), params.get("callback_url"))
                return {"success": True, "message": "", "data": data}
            if normalized == LlmProviderAction.AUTH_STATUS.value:
                data = self.get_session_status(str(params.get("session_id") or ""))
                return {"success": True, "message": "", "data": data}
            if normalized == LlmProviderAction.POLL_AUTH.value:
                data = await self.poll_auth_session(str(params.get("session_id") or ""))
                return {"success": True, "message": "", "data": data}
            if normalized == LlmProviderAction.DISCONNECT.value:
                await self.clear_auth(provider)
                return {"success": True, "message": "", "data": None}
            if normalized == LlmProviderAction.TEST.value:
                return await self._manage_test(provider, **params)
            return {"success": False, "message": f"不支持的管理动作：{normalized}", "data": None}
        except Exception as err:
            return {"success": False, "message": self._sanitize_error(str(err)), "data": None}

    async def _manage_list_models(self, provider: str, **params: Any) -> Dict[str, Any]:
        """管理动作：查询模型目录，附带授权状态摘要。"""
        api_key = params.get("api_key")
        try:
            models = await self.list_models(
                provider_id=provider,
                api_key=api_key,
                base_url=params.get("base_url"),
                base_url_preset_id=params.get("base_url_preset"),
                user_agent=params.get("user_agent"),
                use_proxy=params.get("use_proxy"),
                force_refresh=bool(params.get("force_refresh", False)),
            )
            models = attach_server_tool_capabilities(
                provider=provider,
                models=models,
                base_url=params.get("base_url"),
            )
        except Exception as err:
            return {"success": False, "message": self._sanitize_error(str(err), api_key), "data": None}
        return {
            "success": True,
            "message": "",
            "data": {
                "provider": provider,
                "models": models,
                "auth_status": self.get_auth_status(provider),
            },
        }

    def _requires_api_key(self, provider_id: str) -> bool:
        """判断测试调用是否必须 API Key：支持 OAuth 授权或已有保存凭据的提供商可豁免。"""
        try:
            spec = self.get_provider(provider_id)
        except Exception:
            return True
        if self.get_saved_auth(provider_id):
            return False
        return not spec.oauth_methods

    async def _manage_test(self, provider: str, **params: Any) -> Dict[str, Any]:
        """管理动作：使用传入配置或当前已保存配置执行一次最小 LLM 调用。"""
        from app.agent.llm.helper import LLMHelper, LLMTestTimeout

        provider_name = provider or get_runtime_setting("LLM_PROVIDER")
        model = params.get("model") if params.get("model") is not None else get_runtime_setting("LLM_MODEL")
        enabled = params.get("enabled")
        enabled = bool(enabled) if enabled is not None else bool(get_runtime_setting("AI_AGENT_ENABLE"))
        api_key = params.get("api_key") if params.get("api_key") is not None else get_runtime_setting("LLM_API_KEY")

        data = {"provider": provider_name, "model": model}
        if not provider_name:
            return {"success": False, "message": "请配置LLM提供商和模型", "data": None}
        if not model or not model.strip():
            return {"success": False, "message": "请先配置 LLM 模型", "data": None}
        if not enabled:
            return {"success": False, "message": "请先启用智能助手", "data": data}
        if self._requires_api_key(provider_name) and (not api_key or not api_key.strip()):
            return {"success": False, "message": "请先配置 LLM API Key", "data": data}

        test_kwargs: Dict[str, Any] = {
            "provider": provider_name,
            "model": model,
            "thinking_level": params.get("thinking_level"),
            "api_key": api_key,
            "base_url": params.get("base_url"),
            "base_url_preset": params.get("base_url_preset"),
            "user_agent": params.get("user_agent"),
            "use_proxy": params.get("use_proxy"),
            "api_protocol": params.get("api_protocol"),
            "web_search_mode": params.get("web_search_mode"),
            "provider_runtime": self,
        }
        if params.get("temperature") is not None:
            test_kwargs["temperature"] = params.get("temperature")

        try:
            result = await LLMHelper.test_current_settings(**test_kwargs)
        except (LLMTestTimeout, TimeoutError) as err:
            logger.warning(err)
            return {"success": False, "message": "LLM 调用超时", "data": None}
        except Exception as err:
            return {"success": False, "message": self._sanitize_error(str(err), api_key), "data": None}
        if not result.get("reply_preview"):
            return {"success": False, "message": "模型响应为空", "data": result}
        return {"success": True, "message": "", "data": result}

    @staticmethod
    def _sanitize_error(message: str, api_key: Optional[str] = None) -> str:
        """清理错误信息中的敏感字段，并把 SDK 内部解析错误改写为可定位的基础地址提示。"""
        if not message:
            return "LLM 没有返回任何内容"

        sanitized = message
        if api_key:
            sanitized = sanitized.replace(api_key, "***")
        sanitized = re.sub(
            r"(?i)(api[_-]?key\s*[:=]\s*)([^\s,;]+)",
            r"\1***",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)authorization\s*:\s*bearer\s+[^\s,;]+",
            "Authorization: ***",
            sanitized,
        )

        normalized_message = sanitized.lower().replace("_", "").replace(" ", "")
        if "str" in normalized_message and (
            "modeldump" in normalized_message or "setprivateattributes" in normalized_message
        ):
            return (
                "服务返回内容不是兼容的模型响应，请检查基础地址是否填写为 "
                "API Base URL，如果服务要求 /v1 等版本路径，请包含在基础地址中，"
                "不要填写网页地址或完整的 chat/completions 路径"
            )
        return sanitized

    def resolve_cached_model_metadata(
        self,
        provider_id: str,
        model_id: Optional[str],
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """同步解析缓存中的模型元数据。"""
        return _ProviderCatalog.resolve_cached_model_metadata(
            self,
            provider_id,
            model_id,
            base_url,
            base_url_preset_id,
        )

    def resolve_model_list_base_url(
        self,
        provider_id: str,
        base_url: Optional[str],
        base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        """解析兼容接口用于查询模型列表的基础地址。"""
        return _ProviderCatalog.resolve_model_list_base_url(
            self,
            provider_id,
            base_url,
            base_url_preset_id,
        )

    async def resolve_model_metadata(
        self,
        provider_id: str,
        model_id: Optional[str],
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any] | None:
        """解析模型元数据并按需要刷新公共目录。"""
        return await _ProviderCatalog.resolve_model_metadata(
            self,
            provider_id,
            model_id,
            base_url,
            base_url_preset_id,
            use_proxy,
        )

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
        """解析创建模型客户端所需的统一运行时参数。"""
        return await _ProviderRuntime.resolve_runtime(
            self,
            provider_id,
            model,
            api_key,
            base_url,
            base_url_preset_id,
            user_agent,
            use_proxy,
        )

    async def save_auth(
        self,
        provider_id: str,
        auth_data: dict[str, Any],
    ) -> None:
        """保存 Provider 持久鉴权信息。"""
        return await _ProviderAuth.save_auth(self, provider_id, auth_data)

    async def start_auth(
        self,
        provider_id: str,
        method_id: str,
        callback_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """启动 Provider OAuth 或设备码授权。"""
        return await _ProviderAuth.start_auth(
            self,
            provider_id,
            method_id,
            callback_url,
        )


def render_auth_result_html(success: bool, message: str) -> str:
    """保持 OAuth 回调结果页的稳定 Facade 签名。"""
    return _render_auth_result_html(success, message)
