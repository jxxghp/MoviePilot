"""LLM provider registry, auth flows, and model metadata helpers."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import aiofiles
import httpx
import jwt

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class LLMProviderError(RuntimeError):
    """通用 LLM provider 异常。"""


class LLMProviderAuthError(LLMProviderError):
    """LLM provider 鉴权异常。"""


@dataclass(frozen=True)
class ProviderAuthMethod:
    """前端展示用的授权方式定义。"""

    id: str
    type: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class ProviderUrlPreset:
    """前端展示用的 Base URL 预设。"""

    id: str
    label: str
    value: str
    runtime: Optional[str] = None
    model_list_strategy: Optional[str] = None
    model_list_base_url: Optional[str] = None
    models_dev_provider_id: Optional[str] = None


@dataclass(frozen=True)
class ProviderSpec:
    """描述一个可接入的 LLM provider。"""

    id: str
    name: str
    runtime: str
    models_dev_provider_id: Optional[str] = None
    default_base_url: Optional[str] = None
    base_url_presets: Tuple[ProviderUrlPreset, ...] = ()
    base_url_editable: bool = False
    requires_base_url: bool = False
    supports_api_key: bool = True
    api_key_label: str = "API Key"
    api_key_hint: str = ""
    oauth_methods: Tuple[ProviderAuthMethod, ...] = ()
    supports_model_refresh: bool = True
    model_list_strategy: str = "openai_compatible"
    sort_order: int = 100
    description: str = ""


@dataclass
class PendingAuthSession:
    """保存临时鉴权会话，避免把 PKCE/device code 等状态写回配置。"""

    session_id: str
    provider_id: str
    method_id: str
    flow_type: str
    status: str = "pending"
    message: str = ""
    authorize_url: Optional[str] = None
    instructions: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    interval_seconds: int = 5
    expires_at: float = 0
    created_at: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)


class LLMProviderManager(metaclass=Singleton):
    """统一维护 provider 目录、models.dev 缓存和 OAuth 状态。"""

    _MODELS_DEV_URL = "https://models.dev/api.json"
    _MODELS_DEV_BUNDLED_PATH = Path(__file__).with_name("models.json")
    _MODELS_DEV_CACHE_TTL = 7 * 24 * 60 * 60
    _CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    _CHATGPT_ISSUER = "https://auth.openai.com"
    _CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
    _COPILOT_CLIENT_ID = "Ov23li8tweQw6odWQebz"
    _DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
    _MODELS_DEV_DYNAMIC_SKIP_IDS = {
        "aihubmix",
        "amazon-bedrock",
        "azure",
        "azure-cognitive-services",
        "cloudflare-ai-gateway",
        "cohere",
        "gitlab",
        "google-vertex",
        "google-vertex-anthropic",
        "kiro",
        "sap-ai-core",
        "v0",
        "vercel",
    }
    _PROVIDER_PATCHES = {
        "bailing": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.tbox.cn/api/llm/v1",
            "description": "Bailing OpenAI-compatible 端点。",
        },
        "cerebras": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.cerebras.ai/v1",
            "description": "Cerebras 官方兼容端点。",
        },
        "deepinfra": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.deepinfra.com/v1/openai",
            "description": "DeepInfra 官方兼容端点。",
        },
        "mistral": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.mistral.ai/v1",
            "description": "Mistral 官方兼容端点。",
        },
        "perplexity": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.perplexity.ai/v1",
            "description": "Perplexity 官方兼容端点。",
        },
        "togetherai": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.together.xyz/v1",
            "description": "Together AI 官方兼容端点。",
        },
        "venice": {
            "runtime": "openai_compatible",
            "default_base_url": "https://api.venice.ai/api/v1",
            "description": "Venice AI 官方兼容端点。",
        },
        "cloudflare-workers-ai": {
            "api_key_hint": "填写 Cloudflare API Token，并将 Base URL 中的 ${CLOUDFLARE_ACCOUNT_ID} 替换为真实账户 ID。",
            "description": "Cloudflare Workers AI OpenAI-compatible 端点，需要替换账户 ID。",
        },
        "privatemode-ai": {
            "api_key_hint": "如未启用鉴权，可填写任意占位值。",
            "description": "Privatemode AI 本地 OpenAI-compatible 端点。",
        },
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._models_dev_lock = asyncio.Lock()
        self._pending_sessions: dict[str, PendingAuthSession] = {}
        self._oauth_state_index: dict[str, str] = {}
        self._models_dev_data: dict[str, Any] | None = None
        self._models_dev_loaded_at: float = 0
        self._models_dev_cache_path = (
                Path(settings.TEMP_PATH) / "llm_provider_models_dev_cache.json"
        )

    @staticmethod
    def _builtin_provider_specs() -> tuple[ProviderSpec, ...]:
        """
        返回受支持的 provider 定义。

        OpenAI 保留为用户自定义 OpenAI-compatible 兜底入口，因此仍要求填写
        Base URL；ChatGPT 则单独承接官方 API Key / ChatGPT 订阅鉴权。
        """
        browser_auth = ProviderAuthMethod(
            id="browser_oauth",
            type="oauth",
            label="浏览器授权",
            description="使用 ChatGPT Plus/Pro 浏览器登录并回调授权。",
        )
        device_auth = ProviderAuthMethod(
            id="device_code",
            type="device",
            label="设备码授权",
            description="适合无回调环境，复制设备码到浏览器完成登录。",
        )
        url_preset = ProviderUrlPreset
        provider_patches = LLMProviderManager._PROVIDER_PATCHES
        def openai_provider(
                provider_id: str,
                name: str,
                default_base_url: str,
                sort_order: int,
                *,
                models_dev_provider_id: Optional[str] = None,
                base_url_presets: Tuple[ProviderUrlPreset, ...] = (),
                api_key_hint: Optional[str] = None,
                description: Optional[str] = None,
                model_list_strategy: str = "openai_compatible",
                api_key_label: str = "API Key",
        ) -> ProviderSpec:
            return ProviderSpec(
                id=provider_id,
                name=name,
                runtime="openai_compatible",
                models_dev_provider_id=models_dev_provider_id or provider_id,
                default_base_url=default_base_url,
                base_url_presets=base_url_presets,
                api_key_label=api_key_label,
                api_key_hint=api_key_hint or f"填写 {name} API Key。",
                model_list_strategy=model_list_strategy,
                description=description or f"{name} OpenAI-compatible 端点。",
                sort_order=sort_order,
            )

        def catalog_openai_provider(
                provider_id: str,
                name: str,
                default_base_url: str,
                sort_order: int,
                *,
                models_dev_provider_id: Optional[str] = None,
                base_url_presets: Tuple[ProviderUrlPreset, ...] = (),
                api_key_hint: Optional[str] = None,
                description: Optional[str] = None,
                api_key_label: str = "API Key",
        ) -> ProviderSpec:
            return openai_provider(
                provider_id=provider_id,
                name=name,
                default_base_url=default_base_url,
                sort_order=sort_order,
                models_dev_provider_id=models_dev_provider_id,
                base_url_presets=base_url_presets,
                api_key_hint=api_key_hint,
                description=description,
                model_list_strategy="models_dev_only",
                api_key_label=api_key_label,
            )

        def anthropic_provider(
                provider_id: str,
                name: str,
                default_base_url: str,
                sort_order: int,
                *,
                models_dev_provider_id: Optional[str] = None,
                base_url_presets: Tuple[ProviderUrlPreset, ...] = (),
                api_key_hint: Optional[str] = None,
                description: Optional[str] = None,
        ) -> ProviderSpec:
            return ProviderSpec(
                id=provider_id,
                name=name,
                runtime="anthropic_compatible",
                models_dev_provider_id=models_dev_provider_id or provider_id,
                default_base_url=default_base_url,
                base_url_presets=base_url_presets,
                api_key_hint=api_key_hint or f"填写 {name} API Key。",
                model_list_strategy="anthropic_compatible",
                description=description or f"{name} Anthropic-compatible 端点。",
                sort_order=sort_order,
            )

        catalog_openai_providers = (
            {"id": "huggingface", "name": "Hugging Face", "base_url": "https://router.huggingface.co/v1"},
            {"id": "jiekou", "name": "接口 AI", "base_url": "https://api.jiekou.ai/openai"},
            {"id": "kilo", "name": "Kilo Gateway", "base_url": "https://api.kilo.ai/api/gateway"},
            {"id": "llama", "name": "Llama", "base_url": "https://api.llama.com/compat/v1/"},
            {"id": "llmgateway", "name": "LLM Gateway", "base_url": "https://api.llmgateway.io/v1"},
            {"id": "modelscope", "name": "ModelScope", "base_url": "https://api-inference.modelscope.cn/v1"},
            {"id": "nova", "name": "Nova", "base_url": "https://api.nova.amazon.com/v1"},
            {"id": "fireworks-ai", "name": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1/"},
            {"id": "poe", "name": "Poe", "base_url": "https://api.poe.com/v1"},
            {"id": "qihang-ai", "name": "启航 AI", "base_url": "https://api.qhaigc.net/v1"},
            {"id": "qiniu-ai", "name": "七牛", "base_url": "https://api.qnaigc.com/v1"},
        )

        providers = [
            ProviderSpec(
                id="openai",
                name="OpenAI 兼容",
                runtime="openai_compatible",
                default_base_url="",
                base_url_editable=True,
                requires_base_url=True,
                supports_api_key=True,
                api_key_hint="通用 OpenAI-compatible 兜底入口，需要手动填写 Base URL。",
                description="通用 OpenAI-compatible 模型服务。",
                sort_order=1,
            ),
            ProviderSpec(
                id="chatgpt",
                name="ChatGPT",
                runtime="chatgpt",
                models_dev_provider_id="openai",
                default_base_url="https://api.openai.com/v1",
                api_key_hint="可直接填写 OpenAI API Key，或使用 ChatGPT Plus/Pro 登录授权。",
                oauth_methods=(browser_auth, device_auth),
                model_list_strategy="chatgpt",
                description="支持 ChatGPT Plus/Pro 鉴权或 OpenAI 官方 API Key。",
                sort_order=10,
            ),
            ProviderSpec(
                id="google",
                name="Google",
                runtime="google",
                models_dev_provider_id="google",
                supports_api_key=True,
                api_key_hint="填写 Gemini / Google AI Studio API Key。",
                model_list_strategy="google",
                description="Gemini / Google AI Studio。",
                sort_order=20,
            ),
            anthropic_provider(
                provider_id="anthropic",
                name="Anthropic",
                default_base_url="https://api.anthropic.com/v1",
                sort_order=30,
                api_key_hint="填写 Anthropic API Key。",
                description="Anthropic Claude 官方端点。",
            ),
            ProviderSpec(
                id="deepseek",
                name="DeepSeek",
                runtime="deepseek",
                models_dev_provider_id="deepseek",
                default_base_url="https://api.deepseek.com",
                api_key_hint="填写 DeepSeek API Key。",
                description="DeepSeek 官方平台。",
                sort_order=40,
            ),
            ProviderSpec(
                id="openrouter",
                name="OpenRouter",
                runtime="openai_compatible",
                models_dev_provider_id="openrouter",
                default_base_url="https://openrouter.ai/api/v1",
                api_key_hint="填写 OpenRouter API Key。",
                description="OpenRouter 聚合模型平台。",
                sort_order=50,
            ),
            ProviderSpec(
                id="github-copilot",
                name="GitHub Copilot",
                runtime="github_copilot",
                models_dev_provider_id="github-copilot",
                supports_api_key=False,
                api_key_label="GitHub Token",
                oauth_methods=(
                    ProviderAuthMethod(
                        id="device_code",
                        type="device",
                        label="GitHub 设备码授权",
                        description="使用 GitHub Copilot 订阅登录授权。",
                    ),
                ),
                model_list_strategy="github_copilot",
                description="通过 GitHub Copilot 订阅接入。",
                sort_order=60,
            ),
            catalog_openai_provider(
                provider_id="github-models",
                name="GitHub Models",
                default_base_url="https://models.github.ai/inference",
                sort_order=70,
                api_key_label="GitHub Token",
                api_key_hint="填写具有 GitHub Models 访问权限的 GitHub Token。",
                description="GitHub Models 推理端点。",
            ),
            catalog_openai_provider(
                provider_id="moonshot",
                name="Moonshot / Kimi",
                default_base_url="https://api.moonshot.cn/v1",
                sort_order=80,
                models_dev_provider_id="moonshotai-cn",
                base_url_presets=(
                    url_preset(
                        id="moonshot-cn",
                        label="中国站",
                        value="https://api.moonshot.cn/v1",
                        models_dev_provider_id="moonshotai-cn",
                    ),
                    url_preset(
                        id="moonshot-global",
                        label="国际站",
                        value="https://api.moonshot.ai/v1",
                        models_dev_provider_id="moonshotai",
                    ),
                    url_preset(
                        id="moonshot-kimi-coding",
                        label="Kimi for Coding",
                        value="https://api.kimi.com/coding/v1",
                        runtime="anthropic_compatible",
                        model_list_strategy="anthropic_compatible",
                        models_dev_provider_id="kimi-for-coding",
                    ),
                ),
                api_key_hint="填写 Moonshot / Kimi API Key，可在中国站、国际站与 Kimi for Coding 端点间切换。",
                description="Moonshot / Kimi 官方端点，支持通用 API 与 Kimi for Coding 预设。",
            ),
            anthropic_provider(
                provider_id="minimax",
                name="MiniMax",
                default_base_url="https://api.minimaxi.com/anthropic/v1",
                sort_order=90,
                models_dev_provider_id="minimax-cn",
                base_url_presets=(
                    url_preset(
                        id="minimax-cn-general",
                        label="中国内地 / 通用",
                        value="https://api.minimaxi.com/anthropic/v1",
                        models_dev_provider_id="minimax-cn",
                    ),
                    url_preset(
                        id="minimax-global-general",
                        label="国际站 / 通用",
                        value="https://api.minimax.io/anthropic/v1",
                        models_dev_provider_id="minimax",
                    ),
                    url_preset(
                        id="minimax-cn-coding",
                        label="中国内地 / Coding Plan",
                        value="https://api.minimaxi.com/anthropic/v1",
                        models_dev_provider_id="minimax-cn-coding-plan",
                    ),
                    url_preset(
                        id="minimax-global-coding",
                        label="国际站 / Coding Plan",
                        value="https://api.minimax.io/anthropic/v1",
                        models_dev_provider_id="minimax-coding-plan",
                    ),
                ),
                api_key_hint="填写 MiniMax API Key，可在中国内地、国际站、通用与 Coding Plan 目录间切换。",
                description="MiniMax Anthropic-compatible 端点，支持通用与 Coding Plan 目录预设。",
            ),
            catalog_openai_provider(
                provider_id="xiaomi",
                name="Xiaomi",
                default_base_url="https://api.xiaomimimo.com/v1",
                sort_order=100,
                base_url_presets=(
                    url_preset(
                        id="xiaomi-standard",
                        label="标准端点",
                        value="https://api.xiaomimimo.com/v1",
                        models_dev_provider_id="xiaomi",
                    ),
                    url_preset(
                        id="xiaomi-token-plan-cn",
                        label="Token Plan / 中国",
                        value="https://token-plan-cn.xiaomimimo.com/v1",
                        models_dev_provider_id="xiaomi-token-plan-cn",
                    ),
                    url_preset(
                        id="xiaomi-token-plan-sgp",
                        label="Token Plan / 新加坡",
                        value="https://token-plan-sgp.xiaomimimo.com/v1",
                        models_dev_provider_id="xiaomi-token-plan-sgp",
                    ),
                    url_preset(
                        id="xiaomi-token-plan-ams",
                        label="Token Plan / 欧洲",
                        value="https://token-plan-ams.xiaomimimo.com/v1",
                        models_dev_provider_id="xiaomi-token-plan-ams",
                    ),
                ),
                api_key_hint="填写 Xiaomi API Key，可在标准端点与各区域 Token Plan 端点间切换。",
                description="小米 Mimo 兼容端点。",
            ),
            openai_provider(
                provider_id="zhipu",
                name="智谱 GLM",
                default_base_url="https://open.bigmodel.cn/api/paas/v4",
                sort_order=110,
                models_dev_provider_id="zhipuai",
                base_url_presets=(
                    url_preset(
                        id="zhipu-general",
                        label="Token Plan / 通用 API",
                        value="https://open.bigmodel.cn/api/paas/v4",
                        models_dev_provider_id="zhipuai",
                    ),
                    url_preset(
                        id="zhipu-coding",
                        label="Coding Plan",
                        value="https://open.bigmodel.cn/api/coding/paas/v4",
                        model_list_base_url="https://open.bigmodel.cn/api/paas/v4",
                        models_dev_provider_id="zhipuai-coding-plan",
                    ),
                ),
                api_key_hint="填写智谱开放平台 API Key，可在 Token Plan / 通用 API 与 Coding Plan 端点间切换。",
                description="智谱开放平台国内站，支持通用 API 与 GLM Coding Plan 端点。",
            ),
            openai_provider(
                provider_id="siliconflow",
                name="硅基流动",
                default_base_url="https://api.siliconflow.cn/v1",
                sort_order=120,
                models_dev_provider_id="siliconflow-cn",
                base_url_presets=(
                    url_preset(
                        id="siliconflow-cn",
                        label="中国大陆",
                        value="https://api.siliconflow.cn/v1",
                        models_dev_provider_id="siliconflow-cn",
                    ),
                    url_preset(
                        id="siliconflow-global",
                        label="Global",
                        value="https://api.siliconflow.com/v1",
                        models_dev_provider_id="siliconflow",
                    ),
                ),
                api_key_hint="填写硅基流动 API Key，可在中国大陆与 Global 端点间切换。",
                description="SiliconFlow 官方兼容端点。",
            ),
            openai_provider(
                provider_id="alibaba",
                name="阿里云百炼",
                default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                sort_order=130,
                models_dev_provider_id="alibaba-cn",
                base_url_presets=(
                    url_preset(
                        id="alibaba-cn-general",
                        label="中国内地 / 通用",
                        value="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        models_dev_provider_id="alibaba-cn",
                    ),
                    url_preset(
                        id="alibaba-global-general",
                        label="国际站 / 通用",
                        value="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                        models_dev_provider_id="alibaba",
                    ),
                    url_preset(
                        id="alibaba-cn-coding",
                        label="中国内地 / Coding Plan",
                        value="https://coding.dashscope.aliyuncs.com/v1",
                        model_list_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        models_dev_provider_id="alibaba-coding-plan-cn",
                    ),
                    url_preset(
                        id="alibaba-global-coding",
                        label="国际站 / Coding Plan",
                        value="https://coding-intl.dashscope.aliyuncs.com/v1",
                        model_list_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                        models_dev_provider_id="alibaba-coding-plan",
                    ),
                ),
                api_key_hint="填写 DashScope / Alibaba API Key，可在中国内地、国际站与 Coding Plan 端点间切换。",
                description="阿里云百炼兼容端点。",
            ),
            ProviderSpec(
                id="baidu-qianfan-coding-plan",
                name="百度千帆",
                runtime="openai_compatible",
                default_base_url="https://qianfan.baidubce.com/v2",
                base_url_presets=(
                    url_preset(
                        id="baidu-qianfan-general",
                        label="通用 API",
                        value="https://qianfan.baidubce.com/v2",
                    ),
                    url_preset(
                        id="baidu-qianfan-coding",
                        label="Coding Plan",
                        value="https://qianfan.baidubce.com/v2/coding",
                    ),
                ),
                api_key_hint="填写百度千帆 API Key，可在通用 API 与 Coding Plan 端点间切换。通用 API 请使用 https://qianfan.baidubce.com/v2；Coding Plan 请切换到 https://qianfan.baidubce.com/v2/coding。",
                supports_model_refresh=False,
                description="百度千帆 OpenAI-compatible V2 端点，支持通用 API 与 Coding Plan 地址预设。",
                sort_order=140,
            ),
            ProviderSpec(
                id="jdcloud",
                name="京东云",
                runtime="openai_compatible",
                default_base_url="https://modelservice.jdcloud.com/v1",
                base_url_presets=(
                    url_preset(
                        id="jdcloud-general",
                        label="通用 API",
                        value="https://modelservice.jdcloud.com/v1",
                    ),
                    url_preset(
                        id="jdcloud-coding",
                        label="Coding Plan",
                        value="https://modelservice.jdcloud.com/coding/openai/v1",
                    ),
                ),
                api_key_hint="填写京东云 JoyBuilder API Key。通用 API 请使用 https://modelservice.jdcloud.com/v1；Coding Plan 请切换到 https://modelservice.jdcloud.com/coding/openai/v1，不要把 /v1 当成 Coding Plan 地址。",
                supports_model_refresh=False,
                description="京东云 JoyBuilder OpenAI-compatible 端点，支持通用 API 与 Coding Plan 地址预设。",
                sort_order=150,
            ),
            ProviderSpec(
                id="volcengine",
                name="火山方舟",
                runtime="openai_compatible",
                default_base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key_hint="填写火山方舟 API Key。",
                description="字节跳动火山引擎兼容端点。",
                sort_order=160,
            ),
            ProviderSpec(
                id="tencent",
                name="腾讯云",
                runtime="openai_compatible",
                models_dev_provider_id="tencent-tokenhub",
                default_base_url="https://tokenhub.tencentmaas.com/v1",
                base_url_presets=(
                    url_preset(
                        id="tencent-tokenhub",
                        label="TokenHub",
                        value="https://tokenhub.tencentmaas.com/v1",
                        models_dev_provider_id="tencent-tokenhub",
                    ),
                    url_preset(
                        id="tencent-coding",
                        label="Coding Plan",
                        value="https://api.lkeap.cloud.tencent.com/coding/v3",
                        models_dev_provider_id="tencent-coding-plan",
                    ),
                ),
                api_key_hint="填写 Tencent API Key，可在 TokenHub 与 Coding Plan 端点间切换。",
                model_list_strategy="models_dev_only",
                description="腾讯兼容端点。",
                sort_order=170,
            ),
            ProviderSpec(
                id="ollama-cloud",
                name="Ollama Cloud",
                runtime="openai_compatible",
                models_dev_provider_id="ollama-cloud",
                default_base_url="https://ollama.com/v1",
                api_key_hint="填写 Ollama Cloud API Key。",
                description="Ollama Cloud 云端模型服务。",
                sort_order=180,
            ),
            ProviderSpec(
                id="nvidia",
                name="Nvidia",
                runtime="openai_compatible",
                models_dev_provider_id="nvidia",
                default_base_url="https://integrate.api.nvidia.com/v1",
                api_key_hint="填写 Nvidia API Key。",
                description="Nvidia 集成推理平台。",
                sort_order=190,
            ),
            catalog_openai_provider(
                provider_id="opencode",
                name="OpenCode",
                default_base_url="https://opencode.ai/zen/v1",
                sort_order=200,
                base_url_presets=(
                    url_preset(
                        id="opencode-zen",
                        label="Zen",
                        value="https://opencode.ai/zen/v1",
                        models_dev_provider_id="opencode",
                    ),
                    url_preset(
                        id="opencode-go",
                        label="Go",
                        value="https://opencode.ai/zen/go/v1",
                        models_dev_provider_id="opencode-go",
                    ),
                ),
                api_key_hint="填写 OpenCode API Key，可在 Zen 与 Go 端点间切换。",
                description="OpenCode Zen / Go 端点。",
            ),
            catalog_openai_provider(
                provider_id="groq",
                name="Groq",
                default_base_url="https://api.groq.com/openai/v1",
                sort_order=210,
                api_key_hint="填写 Groq API Key。",
                description="Groq 官方 OpenAI-compatible 端点。",
            ),
            catalog_openai_provider(
                provider_id="xai",
                name="xAI",
                default_base_url="https://api.x.ai/v1",
                sort_order=220,
                api_key_hint="填写 xAI API Key。",
                description="xAI 官方 OpenAI-compatible 端点。",
            ),
            catalog_openai_provider(
                provider_id="zai",
                name="Z.AI",
                default_base_url="https://api.z.ai/api/paas/v4",
                sort_order=230,
                base_url_presets=(
                    url_preset(
                        id="zai-general",
                        label="Token Plan / 通用 API",
                        value="https://api.z.ai/api/paas/v4",
                        models_dev_provider_id="zai",
                    ),
                    url_preset(
                        id="zai-coding",
                        label="Coding Plan",
                        value="https://api.z.ai/api/coding/paas/v4",
                        models_dev_provider_id="zai-coding-plan",
                    ),
                ),
                api_key_hint="填写 Z.AI API Key，可在通用 API 与 Coding Plan 端点间切换。",
                description="Z.AI 官方端点。",
            ),
        ]

        for sort_order, provider_entry in enumerate(
                catalog_openai_providers,
                start=1000,
        ):
            provider_id = provider_entry["id"]
            overrides = provider_patches.get(provider_id, {})
            providers.append(
                catalog_openai_provider(
                    provider_id=provider_id,
                    name=provider_entry["name"],
                    default_base_url=provider_entry["base_url"],
                    sort_order=sort_order,
                    api_key_hint=overrides.get("api_key_hint"),
                    description=overrides.get("description"),
                )
            )
        return tuple(providers)

    def _cached_models_dev_payload(self) -> dict[str, Any]:
        if isinstance(self._models_dev_data, dict):
            return self._models_dev_data

        try:
            if not self._models_dev_cache_path.exists():
                payload = None
            else:
                payload = json.loads(self._models_dev_cache_path.read_text(encoding="utf-8"))
        except Exception as err:
            logger.warning(f"读取 models.dev provider 缓存失败: {err}")
            payload = None

        if not isinstance(payload, dict):
            payload = self._load_bundled_models_dev_payload()

        if not isinstance(payload, dict):
            return {}

        self._models_dev_data = payload
        return payload

    @staticmethod
    def _models_dev_env_names(payload: dict[str, Any]) -> tuple[str, ...]:
        raw_env_names = payload.get("env")
        if not isinstance(raw_env_names, list):
            return ()
        env_names = []
        for item in raw_env_names:
            value = str(item or "").strip()
            if value:
                env_names.append(value)
        return tuple(env_names)

    @classmethod
    def _models_dev_reserved_provider_ids(
            cls, specs: tuple[ProviderSpec, ...]
    ) -> set[str]:
        reserved_ids: set[str] = set()
        for spec in specs:
            if spec.models_dev_provider_id:
                reserved_ids.add(spec.models_dev_provider_id)
            for preset in spec.base_url_presets:
                if preset.models_dev_provider_id:
                    reserved_ids.add(preset.models_dev_provider_id)
        return reserved_ids

    @staticmethod
    def _dynamic_api_key_label(env_names: tuple[str, ...]) -> str:
        first_env = env_names[0].upper() if env_names else ""
        if "TOKEN" in first_env and "KEY" not in first_env:
            return "API Token"
        return "API Key"

    @classmethod
    def _normalize_models_dev_base_url(
            cls, runtime: str, base_url: Optional[str]
    ) -> Optional[str]:
        normalized = cls._sanitize_base_url(base_url)
        if not normalized:
            return None

        suffixes = {
            "openai_compatible": (
                "/chat/completions",
                "/completions",
                "/responses",
                "/embeddings",
                "/audio/speech",
                "/audio/transcriptions",
            ),
            "anthropic_compatible": (
                "/messages",
            ),
        }

        for suffix in suffixes.get(runtime, ()): 
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return cls._sanitize_base_url(normalized)

    @classmethod
    def _models_dev_dynamic_provider_spec(
            cls,
            provider_id: str,
            payload: dict[str, Any],
            sort_order: int,
    ) -> ProviderSpec | None:
        normalized_id = str(provider_id or "").strip().lower()
        if not normalized_id or normalized_id in cls._MODELS_DEV_DYNAMIC_SKIP_IDS:
            return None

        override = cls._PROVIDER_PATCHES.get(normalized_id, {})
        npm_package = str(payload.get("npm") or "").strip()
        runtime = override.get("runtime")
        if not runtime:
            if npm_package == "@ai-sdk/openai-compatible":
                runtime = "openai_compatible"
            elif npm_package == "@ai-sdk/anthropic":
                runtime = "anthropic_compatible"
            else:
                return None

        model_list_strategy = override.get("model_list_strategy")
        if not model_list_strategy:
            model_list_strategy = (
                "anthropic_compatible"
                if runtime == "anthropic_compatible"
                else "models_dev_only"
            )

        default_base_url = cls._normalize_models_dev_base_url(
            runtime=runtime,
            base_url=override.get("default_base_url") or payload.get("api"),
        )
        requires_base_url = not bool(default_base_url)
        env_names = cls._models_dev_env_names(payload)
        api_key_label = override.get("api_key_label") or cls._dynamic_api_key_label(
            env_names
        )
        name = str(payload.get("name") or override.get("name") or normalized_id).strip()
        description = override.get("description")
        if not description:
            transport_name = "Anthropic-compatible" if runtime == "anthropic_compatible" else "OpenAI-compatible"
            description = f"{name} {transport_name} 端点（来自 models.dev 目录）。"

        api_key_hint = override.get("api_key_hint")
        if not api_key_hint:
            api_key_hint = f"填写 {name} {api_key_label}。"
            if requires_base_url:
                api_key_hint = f"填写 {name} {api_key_label}，并手动填写 Base URL。"

        return ProviderSpec(
            id=normalized_id,
            name=name,
            runtime=runtime,
            models_dev_provider_id=normalized_id,
            default_base_url=default_base_url,
            base_url_editable=True,
            requires_base_url=requires_base_url,
            api_key_label=api_key_label,
            api_key_hint=api_key_hint,
            model_list_strategy=model_list_strategy,
            description=description,
            sort_order=sort_order,
        )

    def _dynamic_provider_specs(
            self, builtin_specs: tuple[ProviderSpec, ...]
    ) -> tuple[ProviderSpec, ...]:
        payload = self._cached_models_dev_payload()
        if not payload:
            return ()

        explicit_ids = {spec.id for spec in builtin_specs}
        reserved_ids = self._models_dev_reserved_provider_ids(builtin_specs)
        candidates: list[tuple[str, str, dict[str, Any]]] = []

        for provider_id, provider_payload in payload.items():
            normalized_id = str(provider_id or "").strip().lower()
            if not normalized_id or not isinstance(provider_payload, dict):
                continue
            if normalized_id in explicit_ids or normalized_id in reserved_ids:
                continue

            spec = self._models_dev_dynamic_provider_spec(
                provider_id=normalized_id,
                payload=provider_payload,
                sort_order=0,
            )
            if not spec:
                continue
            candidates.append((spec.name.lower(), normalized_id, provider_payload))

        dynamic_specs = []
        for sort_order, (_, provider_id, provider_payload) in enumerate(
                sorted(candidates),
                start=700,
        ):
            spec = self._models_dev_dynamic_provider_spec(
                provider_id=provider_id,
                payload=provider_payload,
                sort_order=sort_order,
            )
            if not spec:
                continue
            dynamic_specs.append(spec)
        return tuple(dynamic_specs)

    def _provider_specs(self) -> tuple[ProviderSpec, ...]:
        builtin_specs = self._builtin_provider_specs()
        return builtin_specs + self._dynamic_provider_specs(builtin_specs)

    async def _get_provider_async(
            self, provider_id: str, force_refresh: bool = False
    ) -> ProviderSpec:
        normalized_provider_id = self._normalize_provider_id(provider_id)
        try:
            return self.get_provider(normalized_provider_id)
        except LLMProviderError:
            await self.get_models_dev_data(force_refresh=force_refresh)
            return self.get_provider(normalized_provider_id)

    def _serialize_provider(self, spec: ProviderSpec) -> dict[str, Any]:
        return {
            "id": spec.id,
            "name": spec.name,
            "runtime": spec.runtime,
            "default_base_url": self._default_base_url_for_provider(spec) or "",
            "base_url_presets": [
                {
                    "id": preset.id,
                    "label": preset.label,
                    "value": self._sanitize_base_url(preset.value) or "",
                    "runtime": preset.runtime,
                    "model_list_strategy": preset.model_list_strategy,
                }
                for preset in spec.base_url_presets
            ],
            "base_url_editable": spec.base_url_editable,
            "requires_base_url": spec.requires_base_url,
            "supports_api_key": spec.supports_api_key,
            "api_key_label": spec.api_key_label,
            "api_key_hint": spec.api_key_hint,
            "supports_model_refresh": spec.supports_model_refresh,
            "oauth_methods": [
                {
                    "id": method.id,
                    "type": method.type,
                    "label": method.label,
                    "description": method.description,
                }
                for method in spec.oauth_methods
            ],
            "description": spec.description,
            "auth_status": self.get_auth_status(spec.id),
        }

    async def list_providers_async(
            self, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """返回前端可渲染的 provider 目录，并优先补齐 models.dev 动态平台。"""
        try:
            await self.get_models_dev_data(force_refresh=force_refresh)
        except Exception as err:
            logger.debug(f"加载 models.dev provider 目录失败，回退内置列表: {err}")
        return self.list_providers()

    def list_providers(self) -> list[dict[str, Any]]:
        """返回前端可渲染的 provider 目录。"""
        return [
            self._serialize_provider(spec)
            for spec in sorted(self._provider_specs(), key=lambda item: item.sort_order)
        ]

    def get_provider(self, provider_id: str) -> ProviderSpec:
        """按 provider id 获取定义。"""
        normalized = self._normalize_provider_id(provider_id)
        for spec in self._provider_specs():
            if spec.id == normalized:
                return spec
        raise LLMProviderError(f"不支持的 LLM 提供商：{provider_id}")

    @staticmethod
    def _sanitize_base_url(base_url: Optional[str]) -> Optional[str]:
        if base_url is None:
            return None
        value = str(base_url).strip()
        if not value:
            return None
        return value.rstrip("/")

    @classmethod
    def _default_base_url_for_provider(cls, spec: ProviderSpec) -> Optional[str]:
        default_base_url = cls._sanitize_base_url(spec.default_base_url)
        if default_base_url:
            return default_base_url
        if not spec.base_url_presets:
            return None
        return cls._sanitize_base_url(spec.base_url_presets[0].value)

    @classmethod
    def _normalize_provider_id(cls, provider_id: str) -> str:
        normalized = (provider_id or "").strip().lower()
        if normalized == "minimax-coding":
            return "minimax"
        if normalized == "kimi-coding":
            return "moonshot"
        return normalized

    @classmethod
    def _normalize_base_url_preset_id(
            cls, provider_id: str, base_url_preset_id: Optional[str]
    ) -> Optional[str]:
        normalized_provider_id = cls._normalize_provider_id(provider_id)
        normalized_preset_id = str(base_url_preset_id or "").strip().lower() or None
        if not normalized_preset_id:
            return None
        if normalized_provider_id == "minimax" and normalized_preset_id == "minimax-coding":
            return "minimax-cn-coding"
        if normalized_provider_id == "moonshot" and normalized_preset_id == "kimi-coding":
            return "moonshot-kimi-coding"
        return normalized_preset_id

    @classmethod
    def _resolve_provider_preset(
            cls,
            spec: ProviderSpec,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> Optional[ProviderUrlPreset]:
        normalized_preset_id = cls._normalize_base_url_preset_id(spec.id, base_url_preset_id)
        if normalized_preset_id:
            for preset in spec.base_url_presets:
                if preset.id == normalized_preset_id:
                    return preset

        normalized_base_url = cls._sanitize_base_url(base_url)
        if normalized_base_url:
            for preset in spec.base_url_presets:
                preset_value = cls._sanitize_base_url(preset.value)
                if normalized_base_url == preset_value:
                    return preset
            return None

        default_base_url = cls._default_base_url_for_provider(spec)
        if default_base_url:
            for preset in spec.base_url_presets:
                preset_value = cls._sanitize_base_url(preset.value)
                if preset_value == default_base_url:
                    return preset
        return None

    @classmethod
    def _resolve_provider_runtime(
            cls,
            spec: ProviderSpec,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> str:
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        return preset.runtime or spec.runtime if preset else spec.runtime

    @classmethod
    def _resolve_provider_model_list_strategy(
            cls,
            spec: ProviderSpec,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> str:
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        return preset.model_list_strategy or spec.model_list_strategy if preset else spec.model_list_strategy

    @classmethod
    def _resolve_provider_model_list_base_url(
            cls,
            spec: ProviderSpec,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        if preset:
            preset_value = cls._sanitize_base_url(preset.value)
            return cls._sanitize_base_url(preset.model_list_base_url) or preset_value

        normalized_base_url = cls._sanitize_base_url(base_url)
        if normalized_base_url:
            return normalized_base_url

        return cls._default_base_url_for_provider(spec)

    @classmethod
    def _resolve_provider_models_dev_provider_id(
            cls,
            spec: ProviderSpec,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        if preset:
            return preset.models_dev_provider_id or spec.models_dev_provider_id

        normalized_base_url = cls._sanitize_base_url(base_url)
        if normalized_base_url:
            return spec.models_dev_provider_id

        return spec.models_dev_provider_id

    def resolve_model_list_base_url(
            self,
            provider_id: str,
            base_url: Optional[str],
            base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        spec = self.get_provider(provider_id)
        return self._resolve_provider_model_list_base_url(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )

    @staticmethod
    def _httpx_proxy_key() -> str:
        """兼容不同 httpx 版本的 proxy 参数名。"""
        params = httpx.Client.__init__.__code__.co_varnames
        return "proxy" if "proxy" in params else "proxies"

    def _build_httpx_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self._DEFAULT_TIMEOUT}
        if settings.PROXY_HOST:
            kwargs[self._httpx_proxy_key()] = settings.PROXY_HOST
        return kwargs

    @staticmethod
    def _read_agent_config() -> dict[str, Any]:
        config = SystemConfigOper().get(SystemConfigKey.AIAgentConfig)
        if isinstance(config, dict):
            return config
        return {}

    @staticmethod
    async def _write_agent_config(value: dict[str, Any]) -> None:
        """
        使用异步持久化写回 provider 鉴权配置。

        `SystemConfigOper().get()` 读取的是内存缓存，这里保留同步调用；
        但写入需要落库，因此统一走 `async_set()`。
        """
        await SystemConfigOper().async_set(
            SystemConfigKey.AIAgentConfig,
            copy.deepcopy(value) or None,
        )

    def _get_auth_store(self) -> dict[str, Any]:
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if isinstance(auth_store, dict):
            return auth_store
        return {}

    def get_saved_auth(self, provider_id: str) -> dict[str, Any] | None:
        """读取持久化 provider 鉴权信息。"""
        return copy.deepcopy(self._get_auth_store().get(provider_id))

    async def save_auth(self, provider_id: str, auth_data: dict[str, Any]) -> None:
        """写入 provider 鉴权信息。"""
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if not isinstance(auth_store, dict):
            auth_store = {}
        auth_store[provider_id] = copy.deepcopy(auth_data)
        config["provider_auth"] = auth_store
        await self._write_agent_config(config)

    async def clear_auth(self, provider_id: str) -> None:
        """移除 provider 鉴权信息。"""
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if not isinstance(auth_store, dict):
            return
        auth_store.pop(provider_id, None)
        if auth_store:
            config["provider_auth"] = auth_store
        else:
            config.pop("provider_auth", None)
        await self._write_agent_config(config)

    def get_auth_status(self, provider_id: str) -> dict[str, Any]:
        """返回前端展示用的 provider 鉴权摘要。"""
        auth = self.get_saved_auth(provider_id)
        if not auth:
            return {"connected": False}
        return {
            "connected": True,
            "type": auth.get("type"),
            "label": auth.get("label") or auth.get("email") or auth.get("account_id") or "已授权",
            "expires_at": auth.get("expires_at"),
            "updated_at": auth.get("updated_at"),
        }

    async def _load_models_dev_from_disk(self) -> dict[str, Any] | None:
        try:
            if not self._models_dev_cache_path.exists():
                return None
            async with aiofiles.open(
                    self._models_dev_cache_path, mode="r", encoding="utf-8"
            ) as stream:
                return json.loads(await stream.read())
        except Exception as err:
            logger.warning(f"读取 models.dev 缓存失败: {err}")
            return None

    def _load_bundled_models_dev_payload(self) -> dict[str, Any] | None:
        try:
            if not self._MODELS_DEV_BUNDLED_PATH.exists():
                return None
            payload = json.loads(
                self._MODELS_DEV_BUNDLED_PATH.read_text(encoding="utf-8")
            )
        except Exception as err:
            logger.warning(f"读取本地 models.dev 离线文件失败: {err}")
            return None

        return payload if isinstance(payload, dict) else None

    async def _write_models_dev_to_disk(self, payload: dict[str, Any]) -> None:
        try:
            self._models_dev_cache_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(
                    self._models_dev_cache_path, mode="w", encoding="utf-8"
            ) as stream:
                await stream.write(json.dumps(payload, ensure_ascii=False))
        except Exception as err:
            logger.warning(f"写入 models.dev 缓存失败: {err}")

    async def _fetch_models_dev(self) -> dict[str, Any]:
        headers = {"User-Agent": "MoviePilot/1.0"}
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.get(self._MODELS_DEV_URL, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_models_dev_data(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        返回 models.dev 原始数据。

        这里复用 opencode 的做法，把公共模型目录缓存到本地文件中，避免每次
        刷新模型列表都直接打到远端。
        """
        async with self._models_dev_lock:
            now = time.time()
            if (
                    not force_refresh
                    and self._models_dev_data is not None
                    and now - self._models_dev_loaded_at < self._MODELS_DEV_CACHE_TTL
            ):
                return self._models_dev_data

            if not force_refresh and self._models_dev_cache_path.exists():
                mtime = self._models_dev_cache_path.stat().st_mtime
                if now - mtime < self._MODELS_DEV_CACHE_TTL:
                    cached = await self._load_models_dev_from_disk()
                    if isinstance(cached, dict):
                        self._models_dev_data = cached
                        self._models_dev_loaded_at = now
                        return cached

            try:
                payload = await self._fetch_models_dev()
                self._models_dev_data = payload
                self._models_dev_loaded_at = now
                await self._write_models_dev_to_disk(payload)
                return payload
            except Exception as err:
                logger.warning(f"刷新 models.dev 失败，尝试回退本地缓存: {err}")
                cached = await self._load_models_dev_from_disk()
                if isinstance(cached, dict):
                    self._models_dev_data = cached
                    self._models_dev_loaded_at = now
                    return cached
                bundled = self._load_bundled_models_dev_payload()
                if isinstance(bundled, dict):
                    self._models_dev_data = bundled
                    self._models_dev_loaded_at = now
                    return bundled
                raise LLMProviderError(f"获取 models.dev 数据失败: {err}") from err

    async def _models_dev_provider_payload(
            self,
            provider_id: str,
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any]:
        spec = await self._get_provider_async(provider_id)
        models_dev_provider_id = self._resolve_provider_models_dev_provider_id(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if not models_dev_provider_id:
            return {}
        return (await self.get_models_dev_data()).get(models_dev_provider_id, {}) or {}

    async def _models_dev_model(
            self,
            provider_id: str,
            model_id: str,
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        payload = await self._models_dev_provider_payload(
            provider_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return None

        candidates = [model_id]
        if model_id.startswith("models/"):
            candidates.append(model_id.removeprefix("models/"))

        for candidate in candidates:
            if candidate in models:
                return models[candidate]
        return None

    @staticmethod
    def _normalize_model_record(
            model_id: str,
            display_name: Optional[str] = None,
            metadata: Optional[dict[str, Any]] = None,
            transport: str = "openai",
            live_context: Optional[int] = None,
            live_input: Optional[int] = None,
            live_output: Optional[int] = None,
            live_supports_tools: Optional[bool] = None,
            live_supports_reasoning: Optional[bool] = None,
            live_supports_image: Optional[bool] = None,
            live_supports_audio: Optional[bool] = None,
            source: str = "provider",
    ) -> dict[str, Any]:
        """
        统一输出模型记录格式，前端据此直接渲染和自动回填上下文等参数。
        """
        metadata = metadata or {}
        limit = metadata.get("limit") or {}
        modalities = metadata.get("modalities") or {}
        input_modalities = set(modalities.get("input") or [])

        context_tokens = live_context or limit.get("context")
        input_tokens = live_input or limit.get("input")
        output_tokens = live_output or limit.get("output")
        supports_image_input = (
            live_supports_image
            if live_supports_image is not None
            else "image" in input_modalities
        )
        supports_audio_input = (
            live_supports_audio
            if live_supports_audio is not None
            else "audio" in input_modalities
        )
        supports_tools = (
            live_supports_tools
            if live_supports_tools is not None
            else bool(metadata.get("tool_call"))
        )
        supports_reasoning = (
            live_supports_reasoning
            if live_supports_reasoning is not None
            else bool(metadata.get("reasoning"))
        )

        if context_tokens:
            try:
                ct_int = int(context_tokens)
                if ct_int % 1024 == 0 or ct_int == 1048576 or ct_int == 2097152:
                    context_tokens_k = max(1, ct_int // 1024)
                else:
                    context_tokens_k = max(1, (ct_int + 999) // 1000)
            except Exception:
                context_tokens_k = None
        else:
            context_tokens_k = None

        return {
            "id": model_id,
            "name": display_name or metadata.get("name") or model_id,
            "family": metadata.get("family"),
            "context_tokens": context_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "context_tokens_k": context_tokens_k,
            "supports_reasoning": supports_reasoning,
            "supports_tools": supports_tools,
            "supports_image_input": supports_image_input,
            "supports_audio_input": supports_audio_input,
            "transport": transport,
            "source": source,
            "release_date": metadata.get("release_date"),
            "status": metadata.get("status"),
        }

    def _normalize_base_url_for_anthropic(self, base_url: str) -> str:
        normalized = self._sanitize_base_url(base_url) or ""
        if normalized.endswith("/v1"):
            return normalized[:-3]
        return normalized

    async def _list_models_from_google(self, api_key: str) -> list[dict[str, Any]]:
        from google import genai
        from google.genai.types import HttpOptions

        http_options = None
        if settings.PROXY_HOST:
            proxy_key = self._httpx_proxy_key()
            proxy_args = {proxy_key: settings.PROXY_HOST}
            http_options = HttpOptions(
                client_args=proxy_args,
                async_client_args=proxy_args,
            )

        client = genai.Client(api_key=api_key, http_options=http_options)
        response = await client.aio.models.list()
        results = []
        for model in response.page:
            supported = set(model.supported_actions or [])
            if "generateContent" not in supported:
                continue
            model_id = model.name
            metadata = await self._models_dev_model("google", model_id) or {}
            results.append(
                self._normalize_model_record(
                    model_id=model_id,
                    display_name=model.display_name or metadata.get("name") or model_id,
                    metadata=metadata,
                    source="provider",
                )
            )
        return sorted(results, key=lambda item: item["name"].lower())

    async def _list_models_from_openai_compatible(
            self,
            provider_id: str,
            api_key: str,
            base_url: str,
            default_headers: Optional[dict[str, str]] = None,
    ) -> list[dict[str, Any]]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=15.0,
            max_retries=2,
        )
        results = []
        response = await client.models.list()
        for model in response.data:
            metadata = await self._models_dev_model(
                provider_id,
                model.id,
                base_url=base_url,
            ) or {}
            results.append(
                self._normalize_model_record(
                    model_id=model.id,
                    display_name=metadata.get("name") or model.id,
                    metadata=metadata,
                    source="provider",
                )
            )
        return sorted(results, key=lambda item: item["name"].lower())

    async def _list_models_from_models_dev_only(
            self,
            provider_id: str,
            transport: str = "openai",
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        某些 provider 没有统一稳定的 models.list 行为，
        因此优先读取 models.dev 目录；若未来 provider 暴露标准 models 接口，
        再平滑补充实时刷新即可。
        """
        payload = await self._models_dev_provider_payload(
            provider_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            raise LLMProviderError(f"{provider_id} 暂无可用模型目录")
        results = []
        for model_id, metadata in models.items():
            results.append(
                self._normalize_model_record(
                    model_id=model_id,
                    display_name=metadata.get("name") or model_id,
                    metadata=metadata,
                    transport=transport,
                    source="models.dev",
                )
            )
        return sorted(results, key=lambda item: item["name"].lower())

    @staticmethod
    def _copilot_headers(
            token: Optional[str] = None, include_auth: bool = True
    ) -> dict[str, str]:
        """
        构造 GitHub Copilot 请求头。

        OpenAI-compatible 调用会由 SDK 自行补 Authorization，因此这里允许
        仅补充 Copilot 必需的意图头，避免重复覆盖。
        """
        headers = {
            "User-Agent": "MoviePilot/1.0",
            "Openai-Intent": "conversation-edits",
            "x-initiator": "user",
        }
        if include_auth and token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _list_models_from_copilot(self, token: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.get(
                "https://api.githubcopilot.com/models",
                headers=self._copilot_headers(token),
            )
            response.raise_for_status()
            payload = response.json()

        raw_models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            raise LLMProviderError("GitHub Copilot 模型列表响应格式不正确")

        results = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if not item.get("model_picker_enabled", True):
                continue
            if (item.get("policy") or {}).get("state") == "disabled":
                continue

            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue

            endpoints = set(item.get("supported_endpoints") or [])
            # 优先兼容 OpenAI 风格端点；仅在缺失时再切到 Anthropic 风格消息接口。
            transport = (
                "anthropic"
                if "/v1/messages" in endpoints
                   and "/v1/chat/completions" not in endpoints
                   and "/v1/responses" not in endpoints
                else "openai"
            )

            limits = ((item.get("capabilities") or {}).get("limits") or {})
            supports = ((item.get("capabilities") or {}).get("supports") or {})
            metadata = await self._models_dev_model("github-copilot", model_id) or {}
            results.append(
                self._normalize_model_record(
                    model_id=model_id,
                    display_name=item.get("name") or metadata.get("name") or model_id,
                    metadata=metadata,
                    transport=transport,
                    live_context=limits.get("max_context_window_tokens"),
                    live_input=limits.get("max_prompt_tokens"),
                    live_output=limits.get("max_output_tokens"),
                    live_supports_tools=supports.get("tool_calls"),
                    live_supports_reasoning=bool(
                        supports.get("adaptive_thinking")
                        or supports.get("reasoning_effort")
                        or supports.get("max_thinking_budget") is not None
                        or supports.get("min_thinking_budget") is not None
                    ),
                    live_supports_image=bool(
                        supports.get("vision")
                        or ((limits.get("vision") or {}).get("supported_media_types"))
                    ),
                    source="provider",
                )
            )
        return sorted(results, key=lambda i: i["name"].lower())

    async def _list_chatgpt_oauth_models(
            self,
            provider_id: str,
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        # ChatGPT OAuth 仍然是 chatgpt provider 专属能力，但模型目录不再维护
        # 一份内部名单，直接跟随当前 provider 对应的 models.dev 数据。
        payload = await self._models_dev_provider_payload(
            provider_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return []

        results = []
        for model_id, metadata in models.items():
            results.append(
                self._normalize_model_record(
                    model_id=model_id,
                    display_name=metadata.get("name") or model_id,
                    metadata=metadata,
                    source="models.dev",
                )
            )
        return sorted(results, key=lambda item: item["name"].lower())

    async def list_models(
            self,
            provider_id: str,
            api_key: Optional[str] = None,
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
            force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """返回标准化后的模型目录。"""
        spec = await self._get_provider_async(provider_id, force_refresh=force_refresh)
        resolved_model_list_strategy = self._resolve_provider_model_list_strategy(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if self._resolve_provider_models_dev_provider_id(
                spec,
                base_url,
                base_url_preset_id=base_url_preset_id,
        ):
            # 对依赖 models.dev 的 provider 主动刷新一次缓存，保证“刷新模型列表”
            # 在使用目录型 provider 时也能拿到最新参数。
            if force_refresh:
                await self.get_models_dev_data(force_refresh=True)
        runtime = await self.resolve_runtime(
            provider_id,
            model=None,
            api_key=api_key,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )

        if resolved_model_list_strategy == "google":
            return await self._list_models_from_google(runtime["api_key"])

        if resolved_model_list_strategy == "github_copilot":
            return await self._list_models_from_copilot(runtime["api_key"])

        if resolved_model_list_strategy == "chatgpt":
            if runtime.get("auth_mode") == "oauth":
                return await self._list_chatgpt_oauth_models(
                    provider_id=provider_id,
                    base_url=base_url,
                    base_url_preset_id=base_url_preset_id,
                )
            return await self._list_models_from_openai_compatible(
                provider_id="chatgpt",
                api_key=runtime["api_key"],
                base_url=self._resolve_provider_model_list_base_url(
                    spec,
                    runtime["base_url"],
                    base_url_preset_id=base_url_preset_id,
                ),
                default_headers=runtime.get("default_headers"),
            )

        if resolved_model_list_strategy == "anthropic_compatible":
            return await self._list_models_from_models_dev_only(
                provider_id=provider_id,
                transport="anthropic",
                base_url=base_url,
                base_url_preset_id=base_url_preset_id,
            )
            
        if resolved_model_list_strategy == "models_dev_only":
            return await self._list_models_from_models_dev_only(
                provider_id=provider_id,
                transport="openai",
                base_url=base_url,
                base_url_preset_id=base_url_preset_id,
            )

        # openai-compatible / deepseek 默认走官方 models 端点。
        return await self._list_models_from_openai_compatible(
            provider_id=provider_id,
            api_key=runtime["api_key"],
            base_url=self._resolve_provider_model_list_base_url(
                spec,
                runtime["base_url"],
                base_url_preset_id=base_url_preset_id,
            ),
            default_headers=runtime.get("default_headers"),
        )

    async def resolve_model_metadata(
            self,
            provider_id: str,
            model_id: Optional[str],
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        if not model_id:
            return None
        metadata = await self._models_dev_model(
            provider_id,
            model_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if metadata:
            return metadata
        if provider_id == "chatgpt":
            return await self._models_dev_model("openai", model_id)
        if provider_id == "openai":
            models_dev = await self.get_models_dev_data()
            return models_dev.get("openai", {}).get("models", {}).get(model_id)
        return None

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any]:
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except Exception as err:
            print(err)
            return {}

    @staticmethod
    def _extract_chatgpt_account_id(token_payload: dict[str, Any]) -> Optional[str]:
        if token_payload.get("chatgpt_account_id"):
            return token_payload["chatgpt_account_id"]
        auth_payload = token_payload.get("https://api.openai.com/auth") or {}
        if auth_payload.get("chatgpt_account_id"):
            return auth_payload["chatgpt_account_id"]
        organizations = token_payload.get("organizations") or []
        if organizations and isinstance(organizations[0], dict):
            return organizations[0].get("id")
        return None

    def _chatgpt_authorize_url(
            self, redirect_uri: str, challenge: str, state: str
    ) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._CHATGPT_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": "openid profile email offline_access",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": state,
                "originator": "moviepilot",
            }
        )
        return f"{self._CHATGPT_ISSUER}/oauth/authorize?{query}"

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64).replace("=", "")
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return verifier, challenge

    async def start_auth(
            self,
            provider_id: str,
            method_id: str,
            callback_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        启动 OAuth / device code 会话。

        API Key 方式已经由普通设置表单覆盖，这里只处理需要交互式授权的 provider。
        """
        provider = await self._get_provider_async(provider_id)
        method = next(
            (item for item in provider.oauth_methods if item.id == method_id),
            None,
        )
        if not method:
            raise LLMProviderAuthError(f"{provider.name} 不支持授权方式：{method_id}")

        session = PendingAuthSession(
            session_id=secrets.token_urlsafe(18),
            provider_id=provider_id,
            method_id=method_id,
            flow_type=method.type,
            expires_at=time.time() + 600,
        )

        if provider_id == "chatgpt" and method_id == "browser_oauth":
            if not callback_url:
                raise LLMProviderAuthError("ChatGPT 浏览器授权缺少回调地址")
            verifier, challenge = self._pkce_pair()
            state = secrets.token_urlsafe(24)
            session.authorize_url = self._chatgpt_authorize_url(
                redirect_uri=callback_url,
                challenge=challenge,
                state=state,
            )
            session.instructions = "请在浏览器中完成 ChatGPT Plus/Pro 登录授权。"
            session.context.update(
                {
                    "code_verifier": verifier,
                    "state": state,
                    "redirect_uri": callback_url,
                }
            )
            with self._lock:
                self._pending_sessions[session.session_id] = session
                self._oauth_state_index[state] = session.session_id
            return {
                "session_id": session.session_id,
                "flow_type": "oauth_browser",
                "authorize_url": session.authorize_url,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        if provider_id == "chatgpt" and method_id == "device_code":
            async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
                response = await client.post(
                    f"{self._CHATGPT_ISSUER}/api/accounts/deviceauth/usercode",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "MoviePilot/1.0",
                    },
                    json={"client_id": self._CHATGPT_CLIENT_ID},
                )
                response.raise_for_status()
                payload = response.json()

            session.verification_url = f"{self._CHATGPT_ISSUER}/codex/device"
            session.user_code = payload.get("user_code")
            session.interval_seconds = max(int(payload.get("interval") or 5), 1)
            session.instructions = f"请在浏览器输入设备码：{session.user_code}"
            session.context.update(
                {
                    "device_auth_id": payload.get("device_auth_id"),
                    "user_code": payload.get("user_code"),
                }
            )
            with self._lock:
                self._pending_sessions[session.session_id] = session
            return {
                "session_id": session.session_id,
                "flow_type": "device_code",
                "verification_url": session.verification_url,
                "user_code": session.user_code,
                "interval_seconds": session.interval_seconds,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        if provider_id == "github-copilot" and method_id == "device_code":
            async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
                response = await client.post(
                    "https://github.com/login/device/code",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "MoviePilot/1.0",
                    },
                    json={
                        "client_id": self._COPILOT_CLIENT_ID,
                        "scope": "read:user",
                    },
                )
                response.raise_for_status()
                payload = response.json()

            session.verification_url = payload.get("verification_uri")
            session.user_code = payload.get("user_code")
            session.interval_seconds = max(int(payload.get("interval") or 5), 1)
            session.instructions = f"请在 GitHub 页面输入设备码：{session.user_code}"
            session.context.update(
                {
                    "device_code": payload.get("device_code"),
                }
            )
            with self._lock:
                self._pending_sessions[session.session_id] = session
            return {
                "session_id": session.session_id,
                "flow_type": "device_code",
                "verification_url": session.verification_url,
                "user_code": session.user_code,
                "interval_seconds": session.interval_seconds,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        raise LLMProviderAuthError(f"暂未实现 {provider.name} 的授权方式：{method.label}")

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """读取临时授权会话状态。"""
        with self._lock:
            session = self._pending_sessions.get(session_id)
            if not session:
                raise LLMProviderAuthError("授权会话不存在或已过期")
            return {
                "session_id": session.session_id,
                "provider_id": session.provider_id,
                "status": session.status,
                "message": session.message,
                "user_code": session.user_code,
                "verification_url": session.verification_url,
                "authorize_url": session.authorize_url,
                "instructions": session.instructions,
                "interval_seconds": session.interval_seconds,
                "expires_at": session.expires_at,
            }

    async def _mark_session_success(
            self, session: PendingAuthSession, auth_data: dict[str, Any]
    ) -> None:
        auth_data["updated_at"] = int(time.time())
        await self.save_auth(session.provider_id, auth_data)
        session.status = "authorized"
        session.message = "授权成功"

    @staticmethod
    def _mark_session_error(session: PendingAuthSession, message: str) -> None:
        session.status = "failed"
        session.message = message

    async def handle_chatgpt_callback(
            self,
            provider_id: str,
            code: Optional[str],
            state: Optional[str],
            error: Optional[str],
            error_description: Optional[str],
    ) -> tuple[bool, str]:
        """处理 ChatGPT 浏览器 OAuth 回调。"""
        if provider_id != "chatgpt":
            return False, "当前 provider 不支持浏览器 OAuth 回调"

        if error:
            message = error_description or error
            with self._lock:
                session_id = self._oauth_state_index.pop(state or "", None)
                if session_id and session_id in self._pending_sessions:
                    self._mark_session_error(self._pending_sessions[session_id], message)
            return False, message

        if not code or not state:
            return False, "缺少授权码或 state 参数"

        with self._lock:
            session_id = self._oauth_state_index.pop(state, None)
            session = self._pending_sessions.get(session_id or "")

        if not session:
            return False, "授权会话不存在或已失效"

        if state != session.context.get("state"):
            self._mark_session_error(session, "state 校验失败")
            return False, "state 校验失败"

        try:
            payload = await self._exchange_chatgpt_code_for_tokens(
                code=code,
                redirect_uri=session.context["redirect_uri"],
                code_verifier=session.context["code_verifier"],
            )
            claims = self._jwt_claims(payload.get("id_token") or payload["access_token"])
            account_id = self._extract_chatgpt_account_id(claims)
            auth_data = {
                "type": "oauth",
                "provider": "chatgpt",
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "expires_at": int(time.time() + int(payload.get("expires_in") or 3600)),
                "account_id": account_id,
                "email": claims.get("email"),
                "label": claims.get("email") or account_id or "ChatGPT Plus/Pro",
            }
            await self._mark_session_success(session, auth_data)
            return True, "ChatGPT 授权成功"
        except Exception as err:
            message = f"ChatGPT 授权失败: {err}"
            self._mark_session_error(session, message)
            return False, message

    async def poll_auth_session(self, session_id: str) -> dict[str, Any]:
        """
        执行一次 device code 轮询，并返回最新状态。

        前端可按 interval_seconds 轮询，直到状态变为 authorized / failed。
        """
        with self._lock:
            session = self._pending_sessions.get(session_id)
        if not session:
            raise LLMProviderAuthError("授权会话不存在或已过期")
        if session.status != "pending":
            return self.get_session_status(session_id)

        try:
            if session.provider_id == "chatgpt" and session.method_id == "device_code":
                await self._poll_chatgpt_device_auth(session)
            elif session.provider_id == "github-copilot" and session.method_id == "device_code":
                await self._poll_copilot_device_auth(session)
            else:
                raise LLMProviderAuthError("当前授权会话不支持轮询")
        except Exception as err:
            self._mark_session_error(session, str(err))
        return self.get_session_status(session_id)

    async def _exchange_chatgpt_code_for_tokens(
            self, code: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.post(
                f"{self._CHATGPT_ISSUER}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._CHATGPT_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
            )
            response.raise_for_status()
            return response.json()

    async def _refresh_chatgpt_tokens(self, refresh_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.post(
                f"{self._CHATGPT_ISSUER}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._CHATGPT_CLIENT_ID,
                },
            )
            response.raise_for_status()
            return response.json()

    async def _poll_chatgpt_device_auth(self, session: PendingAuthSession) -> None:
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.post(
                f"{self._CHATGPT_ISSUER}/api/accounts/deviceauth/token",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "MoviePilot/1.0",
                },
                json={
                    "device_auth_id": session.context["device_auth_id"],
                    "user_code": session.context["user_code"],
                },
            )

        if response.status_code in {403, 404}:
            session.message = "等待用户在浏览器完成授权"
            return

        response.raise_for_status()
        payload = response.json()
        token_payload = await self._exchange_chatgpt_code_for_tokens(
            code=payload["authorization_code"],
            redirect_uri=f"{self._CHATGPT_ISSUER}/deviceauth/callback",
            code_verifier=payload["code_verifier"],
        )
        claims = self._jwt_claims(
            token_payload.get("id_token") or token_payload["access_token"]
        )
        account_id = self._extract_chatgpt_account_id(claims)
        await self._mark_session_success(
            session,
            {
                "type": "oauth",
                "provider": "chatgpt",
                "access_token": token_payload["access_token"],
                "refresh_token": token_payload["refresh_token"],
                "expires_at": int(time.time() + int(token_payload.get("expires_in") or 3600)),
                "account_id": account_id,
                "email": claims.get("email"),
                "label": claims.get("email") or account_id or "ChatGPT Plus/Pro",
            },
        )

    async def _poll_copilot_device_auth(self, session: PendingAuthSession) -> None:
        async with httpx.AsyncClient(**self._build_httpx_kwargs()) as client:
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "MoviePilot/1.0",
                },
                json={
                    "client_id": self._COPILOT_CLIENT_ID,
                    "device_code": session.context["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            response.raise_for_status()
            payload = response.json()

        access_token = payload.get("access_token")
        if access_token:
            await self._mark_session_success(
                session,
                {
                    "type": "oauth",
                    "provider": "github-copilot",
                    "access_token": access_token,
                    # Copilot 设备码授权返回的是长期可复用 token，这里复用 access 字段即可。
                    "refresh_token": access_token,
                    "expires_at": None,
                    "label": "GitHub Copilot",
                },
            )
            return

        error = payload.get("error")
        if error == "authorization_pending":
            session.message = "等待用户在 GitHub 页面完成授权"
            return
        if error == "slow_down":
            session.interval_seconds = max(session.interval_seconds + 5, 10)
            session.message = "GitHub 要求降低轮询频率，稍后继续。"
            return
        if error:
            raise LLMProviderAuthError(f"GitHub Copilot 授权失败: {error}")

    async def _resolve_chatgpt_oauth(self) -> dict[str, Any]:
        auth = self.get_saved_auth("chatgpt")
        if not auth or auth.get("type") != "oauth":
            raise LLMProviderAuthError("尚未完成 ChatGPT Plus/Pro 授权")

        expires_at = auth.get("expires_at")
        refresh_token = auth.get("refresh_token")
        # 预留 60 秒刷新缓冲，避免刚发起请求就遇到过期。
        if expires_at and refresh_token and int(expires_at) <= int(time.time()) + 60:
            payload = await self._refresh_chatgpt_tokens(refresh_token)
            claims = self._jwt_claims(payload.get("id_token") or payload["access_token"])
            auth.update(
                {
                    "access_token": payload["access_token"],
                    "refresh_token": payload.get("refresh_token") or refresh_token,
                    "expires_at": int(time.time() + int(payload.get("expires_in") or 3600)),
                    "account_id": auth.get("account_id")
                                  or self._extract_chatgpt_account_id(claims),
                    "email": auth.get("email") or claims.get("email"),
                    "label": auth.get("label")
                             or claims.get("email")
                             or auth.get("account_id")
                             or "ChatGPT Plus/Pro",
                }
            )
            await self.save_auth("chatgpt", auth)
        return auth

    async def resolve_runtime(
            self,
            provider_id: str,
            model: Optional[str],
            api_key: Optional[str] = None,
            base_url: Optional[str] = None,
            base_url_preset_id: Optional[str] = None,
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
        spec = await self._get_provider_async(normalized_provider_id)
        resolved_runtime = self._resolve_provider_runtime(
            spec,
            base_url,
            base_url_preset_id=normalized_base_url_preset_id,
        )
        normalized_api_key = str(api_key or "").strip() or None
        normalized_base_url = self._sanitize_base_url(base_url)
        model_record = None
        if model:
            try:
                model_record = next(
                    (
                        item
                        for item in await self.list_models(
                        normalized_provider_id,
                        api_key=api_key,
                        base_url=base_url,
                        base_url_preset_id=normalized_base_url_preset_id,
                    )
                        if item["id"] == model
                    ),
                    None,
                )
            except Exception as err:
                print(err)
                model_record = None

        result: dict[str, Any] = {
            "provider_id": normalized_provider_id,
            "runtime": resolved_runtime,
            "model_id": model,
            "model_record": model_record,
            "model_metadata": await self.resolve_model_metadata(
                normalized_provider_id,
                model,
                base_url=base_url,
                base_url_preset_id=normalized_base_url_preset_id,
            ),
            "default_headers": None,
            "use_responses_api": None,
            "auth_mode": "api_key",
        }

        if normalized_provider_id == "chatgpt":
            auth = None
            try:
                auth = await self._resolve_chatgpt_oauth()
            except Exception as err:
                print(err)
                pass

            if auth:
                headers = {"originator": "moviepilot"}
                if auth.get("account_id"):
                    headers["ChatGPT-Account-Id"] = auth["account_id"]
                result.update(
                    {
                        "runtime": "chatgpt",
                        "api_key": auth["access_token"],
                        "base_url": self._CHATGPT_CODEX_BASE_URL,
                        "default_headers": headers,
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
                        "base_url": normalized_base_url
                                    or self._default_base_url_for_provider(spec),
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
                    "runtime": "copilot_anthropic"
                    if transport == "anthropic"
                    else "github_copilot",
                    "api_key": token,
                    "base_url": "https://api.githubcopilot.com",
                    "default_headers": self._copilot_headers(
                        token,
                        include_auth=transport == "anthropic",
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

        if resolved_runtime == "anthropic_compatible":
            effective_base_url = normalized_base_url or self._default_base_url_for_provider(
                spec
            )
            if not normalized_api_key:
                raise LLMProviderAuthError(f"{spec.name} 需要填写 API Key")
            if not effective_base_url:
                raise LLMProviderAuthError(f"{spec.name} 缺少 Base URL")
            result.update(
                {
                    "api_key": normalized_api_key,
                    "base_url": self._normalize_base_url_for_anthropic(
                        effective_base_url
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
                "auth_mode": "api_key",
            }
        )
        return result


def render_auth_result_html(success: bool, message: str) -> str:
    """OAuth 回调落地页。"""
    title = "授权成功" if success else "授权失败"
    accent = "#3aa675" if success else "#e24b4b"
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #101418;
        color: #f3f5f7;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      .card {{
        width: min(480px, calc(100vw - 32px));
        padding: 28px 24px;
        border-radius: 18px;
        background: rgba(20, 28, 36, 0.92);
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 24px;
        color: {accent};
      }}
      p {{
        margin: 0;
        line-height: 1.7;
        color: #d4dbe3;
      }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>{title}</h1>
      <p>{message}</p>
    </div>
    <script>
      if (window.opener) {{
        try {{
          window.opener.postMessage({json.dumps({"type": "moviepilot-llm-auth", "success": success})}, "*");
        }} catch (err) {{}}
      }}
      setTimeout(() => window.close(), 1800);
    </script>
  </body>
</html>"""
