"""LLM Provider 目录、预设和模型元数据的唯一 owner。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from app.agent.llm.runtime import LLMProviderError
from app.runtime.log import logger


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


class _ProviderCatalog:
    """LLM Provider 目录、预设和模型元数据的唯一 owner。"""

    _models_dev_data: Optional[dict[str, Any]]
    _models_dev_cache_path: Path

    def __getattr__(self, name: str) -> Any:
        """将跨 owner 调用交给最终 Facade 的 MRO 解析。"""
        raise AttributeError(name)

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

    @classmethod
    def _builtin_provider_specs(cls) -> tuple[ProviderSpec, ...]:
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
        provider_patches = cls._PROVIDER_PATCHES

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
                api_key_hint="填写 OpenAI-compatible 服务的 API Key；如服务未启用鉴权，可填写任意占位值。",
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
                id="amazon-bedrock",
                name="Amazon Bedrock",
                runtime="bedrock",
                models_dev_provider_id="amazon-bedrock",
                default_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
                base_url_presets=(
                    url_preset(
                        id="bedrock-us-east-1",
                        label="美东（弗吉尼亚北部）us-east-1",
                        value="https://bedrock-runtime.us-east-1.amazonaws.com",
                    ),
                    url_preset(
                        id="bedrock-us-west-2",
                        label="美西（俄勒冈）us-west-2",
                        value="https://bedrock-runtime.us-west-2.amazonaws.com",
                    ),
                    url_preset(
                        id="bedrock-eu-central-1",
                        label="欧洲（法兰克福）eu-central-1",
                        value="https://bedrock-runtime.eu-central-1.amazonaws.com",
                    ),
                    url_preset(
                        id="bedrock-ap-northeast-1",
                        label="亚太（东京）ap-northeast-1",
                        value="https://bedrock-runtime.ap-northeast-1.amazonaws.com",
                    ),
                    url_preset(
                        id="bedrock-ap-southeast-1",
                        label="亚太（新加坡）ap-southeast-1",
                        value="https://bedrock-runtime.ap-southeast-1.amazonaws.com",
                    ),
                ),
                base_url_editable=True,
                api_key_label="Bedrock API Key / AK:SK",
                api_key_hint=(
                    "支持两种认证方式：填写 Amazon Bedrock API Key（bedrock-api-key- 开头，"
                    "Bearer 认证）；或填写 Access Key ID:Secret Access Key（可选追加 :Session Token，"
                    "SigV4 认证）。Base URL 决定 AWS Region。"
                ),
                model_list_strategy="bedrock",
                description="Amazon Bedrock 托管模型服务，支持 Bedrock API Key 与 AK/SK 双认证。",
                sort_order=35,
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
                id="kuaishou-wanqing",
                name="快手万擎",
                runtime="openai_compatible",
                default_base_url="https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
                base_url_presets=(
                    url_preset(
                        id="kuaishou-wanqing-usage",
                        label="按量计费",
                        value="https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints",
                    ),
                    url_preset(
                        id="kuaishou-wanqing-coding",
                        label="Coding Plan",
                        value="https://wanqing.streamlakeapi.com/api/gateway/coding/v1",
                    ),
                ),
                api_key_hint="填写快手万擎 API Key；模型名称请填写万擎控制台或 OpenClaw 配置中的 model ID。",
                supports_model_refresh=False,
                model_list_strategy="manual",
                description="快手万擎 OpenAI-compatible 端点，支持按量计费与 Coding Plan 地址预设。",
                sort_order=155,
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
                id="china-unicom",
                name="中国联通",
                runtime="openai_compatible",
                default_base_url="https://aigw-gzgy2.cucloud.cn:8443/v1",
                base_url_presets=(
                    url_preset(
                        id="china-unicom-coding-openai",
                        label="Coding Plan / OpenAI",
                        value="https://aigw-gzgy2.cucloud.cn:8443/v1",
                        model_list_strategy="manual",
                    ),
                    url_preset(
                        id="china-unicom-coding-anthropic",
                        label="Coding Plan / Anthropic",
                        value="https://aigw-gzgy2.cucloud.cn:8443",
                        runtime="anthropic_compatible",
                        model_list_strategy="manual",
                    ),
                ),
                base_url_editable=True,
                api_key_hint="填写联通云 AISP / Coding Plan 专属 API Key；模型名称请按控制台可用模型 ID 手动填写。",
                supports_model_refresh=False,
                model_list_strategy="manual",
                description="联通云 AISP Coding Plan 兼容端点，支持 OpenAI 与 Anthropic 协议地址预设。",
                sort_order=172,
            ),
            ProviderSpec(
                id="china-mobile",
                name="中国移动",
                runtime="openai_compatible",
                default_base_url="https://ecloud.10086.cn/api",
                base_url_presets=(
                    url_preset(
                        id="china-mobile-moma",
                        label="MoMA / 移动云",
                        value="https://ecloud.10086.cn/api",
                    ),
                    url_preset(
                        id="china-mobile-coding",
                        label="Coding Plan / 移动智算包",
                        value="https://zhenze-huhehaote.cmecloud.cn/api/coding/v1",
                    ),
                ),
                base_url_editable=True,
                api_key_hint="填写中国移动 MoMA / 移动云 Token 服务 API Key；如控制台下发专属域名，请覆盖 Base URL。",
                supports_model_refresh=False,
                model_list_strategy="manual",
                description="中国移动 MoMA / 移动云 OpenAI-compatible Token 服务，支持专属域名覆盖。",
                sort_order=174,
            ),
            ProviderSpec(
                id="china-telecom",
                name="中国电信",
                runtime="openai_compatible",
                default_base_url="https://wishub-x6.ctyun.cn/v1",
                base_url_presets=(
                    url_preset(
                        id="china-telecom-token-service",
                        label="Token 服务 / 息壤",
                        value="https://wishub-x6.ctyun.cn/v1",
                    ),
                    url_preset(
                        id="china-telecom-coding-openai",
                        label="编码套餐 / OpenAI",
                        value="https://wishub-x6.ctyun.cn/coding/v1",
                        model_list_strategy="manual",
                    ),
                    url_preset(
                        id="china-telecom-coding-anthropic",
                        label="编码套餐 / Anthropic",
                        value="https://wishub-x6.ctyun.cn/coding/v1",
                        runtime="anthropic_compatible",
                        model_list_strategy="manual",
                    ),
                ),
                base_url_editable=True,
                api_key_label="App Key",
                api_key_hint="填写天翼云 Token 服务 / 息壤 App Key；编码套餐模型请按控制台展示的模型 ID 手动填写。",
                description="天翼云 Token 服务（原模型推理服务）OpenAI-compatible 端点，支持通用与编码套餐地址预设。",
                sort_order=176,
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
        """获取缓存的 models.dev payload。"""
        if isinstance(self._models_dev_data, dict):
            return self._models_dev_data

        try:
            if not self._models_dev_cache_path.exists():
                payload = None
            else:
                payload = json.loads(self._models_dev_cache_path.read_text(encoding="utf-8", errors="replace"))
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
        """从 models.dev 数据中提取支持的环境变量名。"""
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
    def _models_dev_reserved_provider_ids(cls, specs: tuple[ProviderSpec, ...]) -> set[str]:
        """获取所有已保留的 models_dev_provider_id 集合。"""
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
        """根据环境变量名动态推断 API Key 标签名称。"""
        first_env = env_names[0].upper() if env_names else ""
        if "TOKEN" in first_env and "KEY" not in first_env:
            return "API Token"
        return "API Key"

    @classmethod
    def _normalize_models_dev_base_url(cls, runtime: str, base_url: Optional[str]) -> Optional[str]:
        """规范化从 models.dev 获取的 Base URL。"""
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
            "anthropic_compatible": ("/messages",),
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
        """根据 models.dev 数据动态生成 ProviderSpec 实例。"""
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
            model_list_strategy = "anthropic_compatible" if runtime == "anthropic_compatible" else "models_dev_only"

        default_base_url = cls._normalize_models_dev_base_url(
            runtime=runtime,
            base_url=override.get("default_base_url") or payload.get("api"),
        )
        requires_base_url = not bool(default_base_url)
        env_names = cls._models_dev_env_names(payload)
        api_key_label = override.get("api_key_label") or cls._dynamic_api_key_label(env_names)
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

    def _dynamic_provider_specs(self, builtin_specs: tuple[ProviderSpec, ...]) -> tuple[ProviderSpec, ...]:
        """获取从 models.dev 动态加载的所有 ProviderSpec 实例。"""
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
        """获取所有支持的 ProviderSpec，包括内置和动态加载的。"""
        builtin_specs = self._builtin_provider_specs()
        return builtin_specs + self._dynamic_provider_specs(builtin_specs)

    async def _get_provider_async(
        self,
        provider_id: str,
        force_refresh: bool = False,
        use_proxy: Optional[bool] = None,
    ) -> ProviderSpec:
        """异步获取指定 provider 的 ProviderSpec 实例。"""
        normalized_provider_id = self._normalize_provider_id(provider_id)
        try:
            return self.get_provider(normalized_provider_id)
        except LLMProviderError:
            await self.get_models_dev_data(
                force_refresh=force_refresh,
                use_proxy=use_proxy,
            )
            return self.get_provider(normalized_provider_id)

    def _serialize_provider(self, spec: ProviderSpec) -> dict[str, Any]:
        """将 ProviderSpec 序列化为前端可用的字典。"""
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
        self,
        force_refresh: bool = False,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """返回前端可渲染的 provider 目录，并优先补齐 models.dev 动态平台。"""
        try:
            await self.get_models_dev_data(
                force_refresh=force_refresh,
                use_proxy=use_proxy,
            )
        except Exception as err:
            logger.debug(f"加载 models.dev provider 目录失败，回退内置列表: {err}")
        return self.list_providers()

    def list_providers(self) -> list[dict[str, Any]]:
        """返回前端可渲染的 provider 目录。"""
        return [
            self._serialize_provider(spec) for spec in sorted(self._provider_specs(), key=lambda item: item.sort_order)
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
        """清理 Base URL 中多余的空格和结尾斜杠。"""
        if base_url is None:
            return None
        value = str(base_url).strip()
        if not value:
            return None
        return value.rstrip("/")

    @staticmethod
    def _merge_user_agent_header(
        default_headers: Optional[dict[str, str]],
        user_agent: Optional[str],
    ) -> Optional[dict[str, str]]:
        """
        合并用户配置的 OpenAI 兼容接口 User-Agent 请求头。
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
    def _default_base_url_for_provider(cls, spec: ProviderSpec) -> Optional[str]:
        """获取 provider 的默认 Base URL。"""
        default_base_url = cls._sanitize_base_url(spec.default_base_url)
        if default_base_url:
            return default_base_url
        if not spec.base_url_presets:
            return None
        return cls._sanitize_base_url(spec.base_url_presets[0].value)

    @classmethod
    def _normalize_provider_id(cls, provider_id: str) -> str:
        """规范化 provider_id 以兼容旧版配置。"""
        normalized = (provider_id or "").strip().lower()
        if normalized == "minimax-coding":
            return "minimax"
        if normalized == "kimi-coding":
            return "moonshot"
        return normalized

    @classmethod
    def _normalize_base_url_preset_id(cls, provider_id: str, base_url_preset_id: Optional[str]) -> Optional[str]:
        """规范化 Base URL 预设 ID。"""
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
        """根据给定的参数解析出适用的 Base URL 预设。"""
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
        """解析提供商最终适用的 runtime。"""
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        return preset.runtime or spec.runtime if preset else spec.runtime

    @classmethod
    def _resolve_provider_model_list_strategy(
        cls,
        spec: ProviderSpec,
        base_url: Optional[str],
        base_url_preset_id: Optional[str] = None,
    ) -> str:
        """解析获取模型列表的策略。"""
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        return preset.model_list_strategy or spec.model_list_strategy if preset else spec.model_list_strategy

    @classmethod
    def _resolve_provider_model_list_base_url(
        cls,
        spec: ProviderSpec,
        base_url: Optional[str],
        base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        """解析用于获取模型列表的 Base URL。"""
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
        """解析对应的 models.dev provider id。"""
        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        if preset:
            return preset.models_dev_provider_id or spec.models_dev_provider_id

        normalized_base_url = cls._sanitize_base_url(base_url)
        if normalized_base_url:
            return spec.models_dev_provider_id

        return spec.models_dev_provider_id

    @classmethod
    def _is_model_profile_endpoint_matched(
        cls,
        spec: ProviderSpec,
        base_url: Optional[str],
        base_url_preset_id: Optional[str] = None,
    ) -> bool:
        """判断模型目录上限是否与当前 provider 端点具有明确对应关系。"""
        if spec.id == "openai":
            return False

        preset = cls._resolve_provider_preset(spec, base_url, base_url_preset_id)
        if preset:
            effective_base_url = cls._sanitize_base_url(base_url) or cls._default_base_url_for_provider(spec)
            preset_base_url = cls._sanitize_base_url(preset.value)
            return effective_base_url == preset_base_url

        default_base_url = cls._default_base_url_for_provider(spec)
        effective_base_url = cls._sanitize_base_url(base_url)
        if not effective_base_url and not default_base_url:
            return bool(spec.models_dev_provider_id)
        effective_base_url = effective_base_url or default_base_url
        return bool(default_base_url and effective_base_url == default_base_url)

    def resolve_model_list_base_url(
        self,
        provider_id: str,
        base_url: Optional[str],
        base_url_preset_id: Optional[str] = None,
    ) -> Optional[str]:
        """解析对外暴露的用于获取模型列表的 Base URL。"""
        spec = self.get_provider(provider_id)
        return self._resolve_provider_model_list_base_url(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )

    async def _models_dev_provider_payload(
        self,
        provider_id: str,
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any]:
        """获取指定 provider 在 models.dev 中的完整负载。"""
        spec = await self._get_provider_async(
            provider_id,
            use_proxy=use_proxy,
        )
        models_dev_provider_id = self._resolve_provider_models_dev_provider_id(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if not models_dev_provider_id:
            return {}
        return (await self.get_models_dev_data(use_proxy=use_proxy)).get(models_dev_provider_id, {}) or {}

    @staticmethod
    def _models_dev_model_candidates(
        provider_id: str,
        model_id: str,
    ) -> tuple[str, ...]:
        """生成模型目录查询候选，兼容 Provider 添加的透明模型前缀。"""
        candidates = [model_id]
        if model_id.startswith("models/"):
            candidates.append(model_id.removeprefix("models/"))
        if provider_id == "amazon-bedrock" and "." in model_id:
            # Cross-region Inference Profile 会增加 us./eu./global. 等前缀。
            candidates.append(model_id.split(".", 1)[1])
        return tuple(dict.fromkeys(candidates))

    async def _models_dev_model(
        self,
        provider_id: str,
        model_id: str,
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any] | None:
        """获取指定模型的 models.dev 元数据。"""
        payload = await self._models_dev_provider_payload(
            provider_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
            use_proxy=use_proxy,
        )
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return None

        for candidate in self._models_dev_model_candidates(provider_id, model_id):
            value = models.get(candidate)
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _metadata_supports_prompt_cache(metadata: Any) -> bool:
        """从统一模型元数据中判断是否声明了提示词缓存能力。"""
        if not isinstance(metadata, dict):
            return False

        explicit_capability = metadata.get("prompt_cache")
        if isinstance(explicit_capability, bool):
            return explicit_capability

        capabilities = metadata.get("capabilities")
        if isinstance(capabilities, dict):
            explicit_capability = capabilities.get("prompt_cache")
            if isinstance(explicit_capability, bool):
                return explicit_capability

        cost = metadata.get("cost")
        return isinstance(cost, dict) and any(key in cost for key in ("cache_read", "cache_write"))

    def _cached_models_dev_model(
        self,
        provider_id: str,
        model_id: str,
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """从已缓存或内置的 models.dev 数据中同步读取模型元数据。"""
        try:
            spec = self.get_provider(provider_id)
        except LLMProviderError:
            return None

        models_dev_provider_id = self._resolve_provider_models_dev_provider_id(
            spec,
            base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if not models_dev_provider_id:
            return None

        payload = self._cached_models_dev_payload().get(models_dev_provider_id, {}) or {}
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return None

        for candidate in self._models_dev_model_candidates(provider_id, model_id):
            value = models.get(candidate)
            if isinstance(value, dict):
                return value
        return None

    def resolve_cached_model_metadata(
        self,
        provider_id: str,
        model_id: Optional[str],
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        """同步解析缓存中的模型元数据，不触发远端 models.dev 刷新。"""
        if not model_id:
            return None
        metadata = self._cached_models_dev_model(
            provider_id,
            model_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
        )
        if metadata:
            return metadata
        if provider_id == "chatgpt":
            return self._cached_models_dev_model("openai", model_id)
        if provider_id == "openai":
            provider_payload = self._cached_models_dev_payload().get("openai", {})
            models = provider_payload.get("models") if isinstance(provider_payload, dict) else None
            value = models.get(model_id) if isinstance(models, dict) else None
            return value if isinstance(value, dict) else None
        return None

    def _resolve_cached_model_record(
        self,
        provider_id: str,
        model_id: Optional[str],
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        transport: str = "openai",
    ) -> dict[str, Any] | None:
        """从缓存中的模型元数据构造轻量模型记录，不触发远端模型列表刷新。"""
        if not model_id:
            return None
        metadata = (
            self.resolve_cached_model_metadata(
                provider_id,
                model_id,
                base_url=base_url,
                base_url_preset_id=base_url_preset_id,
            )
            or {}
        )
        if not metadata:
            return self._normalize_model_record(
                model_id=model_id,
                transport=transport,
                source="configured",
            )
        return self._normalize_model_record(
            model_id=model_id,
            display_name=metadata.get("name") or model_id,
            metadata=metadata,
            transport=transport,
            source="models.dev-cache",
        )

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
        supports_image_input = live_supports_image if live_supports_image is not None else "image" in input_modalities
        supports_audio_input = live_supports_audio if live_supports_audio is not None else "audio" in input_modalities
        supports_tools = live_supports_tools if live_supports_tools is not None else bool(metadata.get("tool_call"))
        supports_reasoning = (
            live_supports_reasoning if live_supports_reasoning is not None else bool(metadata.get("reasoning"))
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

    async def resolve_model_metadata(
        self,
        provider_id: str,
        model_id: Optional[str],
        base_url: Optional[str] = None,
        base_url_preset_id: Optional[str] = None,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any] | None:
        """解析并返回指定模型在 models.dev 中的元数据。"""
        if not model_id:
            return None
        metadata = await self._models_dev_model(
            provider_id,
            model_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
            use_proxy=use_proxy,
        )
        if metadata:
            return metadata
        if provider_id == "chatgpt":
            return await self._models_dev_model(
                "openai",
                model_id,
                use_proxy=use_proxy,
            )
        if provider_id == "openai":
            models_dev = await self.get_models_dev_data(use_proxy=use_proxy)
            provider_payload = models_dev.get("openai", {})
            models = provider_payload.get("models") if isinstance(provider_payload, dict) else None
            value = models.get(model_id) if isinstance(models, dict) else None
            return value if isinstance(value, dict) else None
        return None
