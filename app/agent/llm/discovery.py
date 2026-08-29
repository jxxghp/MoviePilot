"""LLM Provider 远端目录发现和 SDK 客户端 I/O 的唯一 owner。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from app.adapters.network.http import AsyncRequestUtils
from app.agent.llm.runtime import LLMProviderAuthError, LLMProviderError
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


def attach_server_tool_capabilities(
    provider: str,
    models: list[dict[str, Any]],
    base_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """为 Provider 模型目录附加统一的服务端工具能力描述。"""
    from app.agent.llm.server_tools import ServerToolRegistry

    result = []
    for item in models:
        model_item = dict(item)
        model_item["server_tools"] = ServerToolRegistry.list_capabilities(
            provider=provider,
            model=str(model_item.get("id") or ""),
            base_url=base_url,
        )
        result.append(model_item)
    return result


class _ProviderDiscovery:
    """LLM Provider 远端目录发现和 SDK 客户端 I/O 的唯一 owner。"""

    _models_dev_lock: Any
    _models_dev_data: Optional[dict[str, Any]]
    _models_dev_loaded_at: float
    _models_dev_cache_path: Path

    def __getattr__(self, name: str) -> Any:
        """将跨 owner 调用交给最终 Facade 的 MRO 解析。"""
        raise AttributeError(name)

    _MODELS_DEV_URL = "https://models.dev/api.json"

    _MODELS_DEV_BUNDLED_PATH = Path(__file__).with_name("models.json")

    _MODELS_DEV_CACHE_TTL = 7 * 24 * 60 * 60

    _BEDROCK_DEFAULT_REGION = "us-east-1"

    _BEDROCK_API_KEY_PREFIX = "bedrock-api-key-"

    _BEDROCK_GPT_OSS_BASE_REGIONS = (
        "ap-northeast-1",
        "ap-south-1",
        "ap-southeast-2",
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "eu-west-2",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    )

    _BEDROCK_GPT_OSS_SAFEGUARD_REGIONS = (
        "ap-northeast-1",
        "ap-south-1",
        "ap-southeast-2",
        "eu-west-1",
        "eu-west-2",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    )

    _BEDROCK_ON_DEMAND_MODEL_REGIONS = {
        "openai.gpt-oss-120b-1:0": _BEDROCK_GPT_OSS_BASE_REGIONS,
        "openai.gpt-oss-20b-1:0": _BEDROCK_GPT_OSS_BASE_REGIONS,
        "openai.gpt-oss-safeguard-120b": _BEDROCK_GPT_OSS_SAFEGUARD_REGIONS,
        "openai.gpt-oss-safeguard-20b": _BEDROCK_GPT_OSS_SAFEGUARD_REGIONS,
        "amazon.nova-lite-v1:0": (
            "ap-northeast-1",
            "ap-southeast-2",
            "eu-west-2",
            "us-east-1",
            "us-gov-west-1",
        ),
        "amazon.nova-micro-v1:0": (
            "ap-southeast-2",
            "eu-west-2",
            "us-east-1",
            "us-gov-west-1",
        ),
        "amazon.nova-pro-v1:0": (
            "ap-southeast-2",
            "eu-west-2",
            "us-east-1",
            "us-gov-west-1",
        ),
        "anthropic.claude-3-5-haiku-20241022-v1:0": ("us-west-2",),
        "anthropic.claude-3-5-sonnet-20240620-v1:0": (
            "ap-northeast-1",
            "ap-northeast-2",
            "ap-southeast-1",
            "eu-central-1",
            "eu-central-2",
            "us-east-1",
            "us-gov-west-1",
            "us-west-2",
        ),
        "anthropic.claude-3-5-sonnet-20241022-v2:0": (
            "ap-southeast-2",
            "us-west-2",
        ),
        "anthropic.claude-3-7-sonnet-20250219-v1:0": (
            "eu-west-2",
            "us-gov-west-1",
        ),
        "anthropic.claude-3-haiku-20240307-v1:0": (
            "ap-northeast-1",
            "ap-northeast-2",
            "ap-south-1",
            "ap-southeast-2",
            "eu-central-1",
            "eu-west-1",
            "eu-west-3",
            "us-east-1",
            "us-gov-west-1",
            "us-west-2",
        ),
    }

    _BEDROCK_GEO_PREFIXES: dict[str, tuple[str, ...]] = {
        "us": ("us-east-", "us-west-"),
        "eu": ("eu-",),
        "apac": ("ap-",),
        "au": ("ap-southeast-2", "ap-southeast-4"),
        "jp": ("ap-northeast-1", "ap-northeast-3"),
        "ca": ("ca-",),
    }

    _BEDROCK_NON_COMMERCIAL_REGION_PREFIXES = (
        "cn-",
        "eu-isoe-",
        "us-gov-",
        "us-iso-",
        "us-isob-",
        "us-isof-",
    )

    def _build_async_request(
        self,
        use_proxy: Optional[bool] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> AsyncRequestUtils:
        """按 LLM 代理策略构造统一异步 HTTP 请求客户端。"""
        should_use_proxy = get_runtime_setting("LLM_USE_PROXY") if use_proxy is None else use_proxy
        proxy = get_runtime_setting("PROXY_HOST") if should_use_proxy else None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return AsyncRequestUtils(
            headers=headers,
            proxies=proxies,
            timeout=15,
            verify=True,
            trust_env=False,
        )

    def _build_sdk_http_client(self, use_proxy: Optional[bool] = None) -> Any:
        """为需要注入 transport 的第三方 SDK 构造统一异步客户端。"""
        should_use_proxy = get_runtime_setting("LLM_USE_PROXY") if use_proxy is None else use_proxy
        proxy = get_runtime_setting("PROXY_HOST") if should_use_proxy else None
        return AsyncRequestUtils.create_sdk_client(
            proxy=proxy,
            timeout=15,
            connect_timeout=10,
            trust_env=False,
        )

    async def _load_models_dev_from_disk(self) -> dict[str, Any] | None:
        """从磁盘缓存加载 models.dev 数据。"""
        try:
            if not self._models_dev_cache_path.exists():
                return None
            content = await asyncio.to_thread(
                self._models_dev_cache_path.read_text,
                encoding="utf-8",
                errors="replace",
            )
            payload = json.loads(content)
            return payload if isinstance(payload, dict) else None
        except Exception as err:
            logger.warning(f"读取 models.dev 缓存失败: {err}")
            return None

    def _load_bundled_models_dev_payload(self) -> dict[str, Any] | None:
        """从随代码附带的离线文件加载 models.dev 数据。"""
        try:
            if not self._MODELS_DEV_BUNDLED_PATH.exists():
                return None
            payload = json.loads(self._MODELS_DEV_BUNDLED_PATH.read_text(encoding="utf-8", errors="replace"))
        except Exception as err:
            logger.warning(f"读取本地 models.dev 离线文件失败: {err}")
            return None

        return payload if isinstance(payload, dict) else None

    async def _write_models_dev_to_disk(self, payload: dict[str, Any]) -> None:
        """将 models.dev 数据写入磁盘缓存。"""
        try:
            self._models_dev_cache_path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(payload, ensure_ascii=False)
            await asyncio.to_thread(
                self._models_dev_cache_path.write_text,
                content,
                encoding="utf-8",
            )
        except Exception as err:
            logger.warning(f"写入 models.dev 缓存失败: {err}")

    async def _fetch_models_dev(self, use_proxy: Optional[bool] = None) -> dict[str, Any]:
        """通过网络请求获取最新 models.dev 数据。"""
        response = await self._build_async_request(
            use_proxy,
            headers={"User-Agent": get_runtime_setting("USER_AGENT")},
        ).get_res(self._MODELS_DEV_URL, raise_exception=True)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise LLMProviderError("models.dev 返回了无效目录响应")
        return payload

    async def get_models_dev_data(
        self,
        force_refresh: bool = False,
        use_proxy: Optional[bool] = None,
    ) -> dict[str, Any]:
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
                payload = await self._fetch_models_dev(use_proxy=use_proxy)
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

    def _normalize_base_url_for_anthropic(self, base_url: str) -> str:
        """对 Anthropic 的 Base URL 进行适配处理。"""
        normalized = self._sanitize_base_url(base_url) or ""
        if normalized.endswith("/v1"):
            return normalized[:-3]
        return normalized

    @classmethod
    def _extract_bedrock_region(cls, base_url: Optional[str]) -> str:
        """
        从 Bedrock 运行时端点 URL 中提取 AWS Region

        兼容标准端点、FIPS 端点与 PrivateLink（VPCE）端点等主机名形态，
        从中识别 Region 段。

        :param base_url: 形如 https://bedrock-runtime.us-east-1.amazonaws.com 的端点地址
        :return: 提取到的 Region，无法识别时回退 us-east-1
        """
        hostname = urlsplit((base_url or "").strip().lower()).hostname or ""
        match = re.search(
            r"(?:^|\.)(?:bedrock(?:-runtime)?(?:-fips)?)"
            r"\.([a-z0-9-]+-\d+)(?:\.|$)",
            hostname,
        )
        if match:
            return match.group(1)
        return cls._BEDROCK_DEFAULT_REGION

    @classmethod
    def _bedrock_model_matches_region(cls, model_id: str, region: str) -> bool:
        """
        判断目录中的模型 ID 在指定 Region 是否可调用

        models.dev 目录同时收录裸模型 ID（直连调用）与带地理前缀的
        Inference Profile ID（us./eu./apac./global. 等）。带前缀的条目只在
        对应地理分区和 AWS 分区的 Region 可用；global Profile 仅允许商业
        AWS 分区。裸 ID 仅在明确记录的 ON_DEMAND Region 可用，未知条目
        按不可直连处理。

        :param model_id: 目录中的模型 ID
        :param region: 当前 Base URL 对应的 AWS Region
        :return: 该模型在当前 Region 可调用时返回 True
        """
        prefix = model_id.split(".", 1)[0]
        if prefix == "global":
            return not region.startswith(cls._BEDROCK_NON_COMMERCIAL_REGION_PREFIXES)
        region_prefixes = cls._BEDROCK_GEO_PREFIXES.get(prefix)
        if region_prefixes is not None:
            return not region.startswith(cls._BEDROCK_NON_COMMERCIAL_REGION_PREFIXES) and region.startswith(
                region_prefixes
            )
        on_demand_regions = cls._BEDROCK_ON_DEMAND_MODEL_REGIONS.get(model_id)
        return on_demand_regions is not None and region in on_demand_regions

    @classmethod
    def _parse_bedrock_credentials(cls, api_key: Optional[str]) -> dict[str, Any]:
        """
        解析 Bedrock 凭证字符串，识别 Bearer 与 SigV4 两种认证方式

        - Bedrock API Key（bedrock-api-key- 开头的长期 Key，或控制台生成的短期
          Token）走 Bearer 认证；
        - `AccessKeyId:SecretAccessKey` 或 `AccessKeyId:SecretAccessKey:SessionToken`
          走 SigV4 认证，AWS Access Key ID 均以 "AKIA"/"ASIA" 开头。

        :param api_key: 用户在 API Key 输入框填写的凭证内容
        :return: 含 auth_scheme 及对应凭证字段的字典
        """
        normalized = str(api_key or "").strip()
        if not normalized:
            raise LLMProviderAuthError("Amazon Bedrock 需要填写 Bedrock API Key 或 Access Key ID:Secret Access Key")

        if not normalized.startswith(cls._BEDROCK_API_KEY_PREFIX):
            parts = [part.strip() for part in normalized.split(":")]
            if len(parts) in {2, 3} and all(parts):
                credentials = {
                    "auth_scheme": "sigv4",
                    "access_key_id": parts[0],
                    "secret_access_key": parts[1],
                }
                if len(parts) == 3:
                    credentials["session_token"] = parts[2]
                return credentials
            if ":" in normalized:
                raise LLMProviderAuthError(
                    "Amazon Bedrock AK/SK 凭证格式不正确，"
                    "请按 AccessKeyId:SecretAccessKey 或 "
                    "AccessKeyId:SecretAccessKey:SessionToken 填写"
                )

        return {"auth_scheme": "bearer", "bearer_token": normalized}

    async def _list_models_from_google(
        self,
        api_key: str,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """从 Google AI Studio 获取模型列表。"""
        from google import genai
        from google.genai.types import HttpOptions

        should_use_proxy = get_runtime_setting("LLM_USE_PROXY") if use_proxy is None else use_proxy
        proxy = get_runtime_setting("PROXY_HOST") if should_use_proxy else None
        client_args = AsyncRequestUtils.build_sdk_client_args(
            proxy=proxy,
            timeout=15,
            connect_timeout=10,
            trust_env=False,
        )
        http_options = HttpOptions(
            client_args=client_args,
            async_client_args=client_args,
        )

        client = genai.Client(api_key=api_key, http_options=http_options)
        try:
            response = await client.aio.models.list()
            results = []
            for model in response.page:
                supported = set(model.supported_actions or [])
                if "generateContent" not in supported:
                    continue
                model_id = model.name
                metadata = (
                    await self._models_dev_model(
                        "google",
                        model_id,
                        use_proxy=use_proxy,
                    )
                    or {}
                )
                results.append(
                    self._normalize_model_record(
                        model_id=model_id,
                        display_name=model.display_name or metadata.get("name") or model_id,
                        metadata=metadata,
                        source="provider",
                    )
                )
            return sorted(results, key=lambda item: item["name"].lower())
        finally:
            try:
                await client.aio.aclose()
            finally:
                client.close()

    async def _list_models_from_openai_compatible(
        self,
        provider_id: str,
        api_key: str,
        base_url: str,
        default_headers: Optional[dict[str, str]] = None,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """通过 OpenAI 兼容接口获取模型列表。"""
        from openai import AsyncOpenAI

        async with AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=15.0,
            max_retries=2,
            http_client=self._build_sdk_http_client(use_proxy),
        ) as client:
            results = []
            response = await client.models.list()
            for model in response.data:
                metadata = (
                    await self._models_dev_model(
                        provider_id,
                        model.id,
                        base_url=base_url,
                        use_proxy=use_proxy,
                    )
                    or {}
                )
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
        use_proxy: Optional[bool] = None,
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
            use_proxy=use_proxy,
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

    def _build_bedrock_boto3_config(
        self,
        use_proxy: Optional[bool] = None,
    ) -> Any:
        """
        构造 Bedrock boto3 客户端配置，统一超时、重试与代理策略

        :param use_proxy: 是否使用系统代理，None 时读取 LLM_USE_PROXY 配置
        :return: botocore Config 实例
        """
        from botocore.config import Config

        should_use_proxy = get_runtime_setting("LLM_USE_PROXY") if use_proxy is None else use_proxy
        proxies = None
        if should_use_proxy and get_runtime_setting("PROXY_HOST"):
            proxies = {"http": get_runtime_setting("PROXY_HOST"), "https": get_runtime_setting("PROXY_HOST")}
        return Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
            proxies=proxies,
        )

    @staticmethod
    def _bedrock_endpoint_url(service_name: str, base_url: Optional[str]) -> Optional[str]:
        """
        解析应传给 boto3 客户端的自定义端点 URL

        标准公有端点交由 boto3 按 Region 自行推导；用户填写 PrivateLink、
        FIPS 等非标准端点时才显式透传，保证所选网络路径实际生效。

        :param service_name: boto3 服务名（bedrock 或 bedrock-runtime）
        :param base_url: 用户配置的 Base URL
        :return: 需要显式指定端点时返回 URL，否则返回 None
        """
        normalized = (base_url or "").strip().rstrip("/")
        if not normalized:
            return None
        if re.fullmatch(
            rf"https://{service_name}\.[a-z0-9-]+\.amazonaws\.com",
            normalized,
        ):
            return None
        return normalized

    def create_bedrock_client(
        self,
        service_name: str,
        region: str,
        credentials: dict[str, Any],
        base_url: Optional[str] = None,
        use_proxy: Optional[bool] = None,
        read_timeout: Optional[int] = None,
    ) -> Any:
        """
        按解析后的凭证创建 Bedrock boto3 客户端，Bearer 方式注入 Authorization 头

        :param service_name: boto3 服务名（bedrock 或 bedrock-runtime）
        :param region: AWS Region
        :param credentials: `_parse_bedrock_credentials` 的解析结果
        :param base_url: 用户配置的 Base URL，非标准端点（PrivateLink/FIPS 等）时透传给 boto3
        :param use_proxy: 是否使用系统代理
        :param read_timeout: 读取超时秒数，None 时使用默认值
        :return: boto3 客户端实例
        """
        import boto3
        from botocore import UNSIGNED

        config = self._build_bedrock_boto3_config(use_proxy)
        if read_timeout:
            config = config.merge(type(config)(read_timeout=read_timeout))
        endpoint_kwargs: dict[str, Any] = {}
        endpoint_url = self._bedrock_endpoint_url(service_name, base_url)
        if endpoint_url:
            endpoint_kwargs["endpoint_url"] = endpoint_url

        if credentials["auth_scheme"] == "sigv4":
            return boto3.client(
                service_name,
                region_name=region,
                aws_access_key_id=credentials["access_key_id"],
                aws_secret_access_key=credentials["secret_access_key"],
                aws_session_token=credentials.get("session_token"),
                config=config,
                **endpoint_kwargs,
            )

        # Bearer 认证：以 UNSIGNED 跳过 SigV4 签名，再把 API Key 注入 Authorization 头。
        bearer_token = credentials["bearer_token"]
        config = config.merge(type(config)(signature_version=UNSIGNED))
        client = boto3.client(
            service_name,
            region_name=region,
            aws_access_key_id="unsigned",
            aws_secret_access_key="unsigned",
            config=config,
            **endpoint_kwargs,
        )

        def _inject_bearer(request: Any, **_kwargs: Any) -> None:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register(
            f"request-created.{service_name}",
            _inject_bearer,
        )
        return client

    async def _list_models_from_bedrock_fallback(
        self,
        region: str,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """
        从 models.dev 目录筛选当前 Region 可调用的 Bedrock 模型

        :param region: 当前 Base URL 对应的 AWS Region
        :param use_proxy: 是否使用系统代理
        :return: 过滤后的标准化模型记录列表
        """
        models = await self._list_models_from_models_dev_only(
            provider_id="amazon-bedrock",
            use_proxy=use_proxy,
        )
        return [model for model in models if self._bedrock_model_matches_region(model["id"], region)]

    async def _list_models_from_bedrock(
        self,
        api_key: str,
        base_url: Optional[str],
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """
        从 Bedrock 控制面拉取模型目录，聚合跨区 Inference Profile 与直连模型

        Bedrock 多数新模型仅允许通过 Inference Profile（us./eu./apac./global. 前缀）
        调用，因此优先列出 Profile，再补充支持 ON_DEMAND 直连的基础模型。

        :param api_key: 用户填写的凭证内容（Bedrock API Key 或 AK/SK）
        :param base_url: Bedrock 运行时端点，决定 Region
        :param use_proxy: 是否使用系统代理
        :return: 标准化后的模型记录列表
        """
        credentials = self._parse_bedrock_credentials(api_key)
        region = self._extract_bedrock_region(base_url)
        # runtime VPCE 无法安全推导对应的控制面 VPCE；FIPS 端点也不能绕回
        # 公有非 FIPS 控制面，因此直接使用本地目录。
        if self._bedrock_endpoint_url("bedrock-runtime", base_url):
            return await self._list_models_from_bedrock_fallback(region, use_proxy)
        client = self.create_bedrock_client(
            "bedrock",
            region=region,
            credentials=credentials,
            use_proxy=use_proxy,
        )

        def _fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            profiles: list[dict[str, Any]] = []
            paginator = client.get_paginator("list_inference_profiles")
            for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
                profiles.extend(page.get("inferenceProfileSummaries") or [])
            foundation = (
                client.list_foundation_models(
                    byOutputModality="TEXT",
                    byInferenceType="ON_DEMAND",
                ).get("modelSummaries")
                or []
            )
            return profiles, foundation

        try:
            profile_summaries, foundation_summaries = await asyncio.to_thread(_fetch)
        except Exception as err:
            # 部分 Bedrock API Key 的授权范围仅覆盖 bedrock-runtime 推理接口，
            # 控制面查询被拒时降级到 models.dev 目录，保证仍能选择模型。
            logger.warning(f"获取 Amazon Bedrock 控制面模型列表失败，降级 models.dev 目录: {err}")
            return await self._list_models_from_bedrock_fallback(region, use_proxy)
        finally:
            await asyncio.to_thread(client.close)

        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def _append_record(model_id: str, display_name: Optional[str]) -> None:
            if not model_id or model_id in seen_ids:
                return
            seen_ids.add(model_id)
            # Inference Profile 带区域前缀，models.dev 目录按基础模型 ID 收录，
            # 去掉首个前缀段再查一次元数据。
            metadata = self._cached_models_dev_model("amazon-bedrock", model_id)
            if not metadata and "." in model_id:
                metadata = self._cached_models_dev_model(
                    "amazon-bedrock",
                    model_id.split(".", 1)[1],
                )
            results.append(
                self._normalize_model_record(
                    model_id=model_id,
                    display_name=display_name or (metadata or {}).get("name") or model_id,
                    metadata=metadata or {},
                    source="provider",
                )
            )

        for profile in profile_summaries:
            if (profile.get("status") or "ACTIVE") != "ACTIVE":
                continue
            _append_record(
                str(profile.get("inferenceProfileId") or "").strip(),
                profile.get("inferenceProfileName"),
            )
        # 控制面已按当前 Region 和 ON_DEMAND 筛选，不能复用仅面向
        # models.dev 降级目录的静态白名单，否则 AWS 新增模型会被遗漏。
        for summary in foundation_summaries:
            lifecycle = (summary.get("modelLifecycle") or {}).get("status") or "ACTIVE"
            if lifecycle != "ACTIVE":
                continue
            _append_record(
                str(summary.get("modelId") or "").strip(),
                summary.get("modelName"),
            )

        return sorted(results, key=lambda item: item["name"].lower())

    @staticmethod
    def _copilot_headers(token: Optional[str] = None, include_auth: bool = True) -> dict[str, str]:
        """
        构造 GitHub Copilot 请求头。

        OpenAI-compatible 调用会由 SDK 自行补 Authorization，因此这里允许
        仅补充 Copilot 必需的意图头，避免重复覆盖。
        """
        headers = {
            "User-Agent": get_runtime_setting("USER_AGENT"),
            "Openai-Intent": "conversation-edits",
            "x-initiator": "user",
        }
        if include_auth and token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _list_models_from_copilot(
        self,
        token: str,
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """从 GitHub Copilot 端点获取模型列表。"""
        response = await self._build_async_request(
            use_proxy,
            headers=self._copilot_headers(token),
        ).get_res(
            "https://api.githubcopilot.com/models",
            raise_exception=True,
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

            limits = (item.get("capabilities") or {}).get("limits") or {}
            supports = (item.get("capabilities") or {}).get("supports") or {}
            metadata = (
                await self._models_dev_model(
                    "github-copilot",
                    model_id,
                    use_proxy=use_proxy,
                )
                or {}
            )
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
                        supports.get("vision") or ((limits.get("vision") or {}).get("supported_media_types"))
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
        use_proxy: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """获取开启 OAuth 的 ChatGPT 模型列表。"""
        # ChatGPT OAuth 仍然是 chatgpt provider 专属能力，但模型目录不再维护
        # 一份内部名单，直接跟随当前 provider 对应的 models.dev 数据。
        payload = await self._models_dev_provider_payload(
            provider_id,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
            use_proxy=use_proxy,
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
        user_agent: Optional[str] = None,
        use_proxy: Optional[bool] = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """返回标准化后的模型目录。"""
        spec = await self._get_provider_async(
            provider_id,
            force_refresh=force_refresh,
            use_proxy=use_proxy,
        )
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
                await self.get_models_dev_data(
                    force_refresh=True,
                    use_proxy=use_proxy,
                )

        if resolved_model_list_strategy == "manual":
            # 万擎等推理点型平台没有稳定的全局模型目录，模型 ID 需要用户从控制台复制。
            return []

        runtime = await self.resolve_runtime(
            provider_id,
            model=None,
            api_key=api_key,
            base_url=base_url,
            base_url_preset_id=base_url_preset_id,
            user_agent=user_agent,
            use_proxy=use_proxy,
        )

        if resolved_model_list_strategy == "google":
            return await self._list_models_from_google(
                runtime["api_key"],
                use_proxy=use_proxy,
            )

        if resolved_model_list_strategy == "github_copilot":
            return await self._list_models_from_copilot(
                runtime["api_key"],
                use_proxy=use_proxy,
            )

        if resolved_model_list_strategy == "chatgpt":
            if runtime.get("auth_mode") == "oauth":
                return await self._list_chatgpt_oauth_models(
                    provider_id=provider_id,
                    base_url=base_url,
                    base_url_preset_id=base_url_preset_id,
                    use_proxy=use_proxy,
                )
            return await self._list_models_from_openai_compatible(
                provider_id="chatgpt",
                api_key=runtime["api_key"],
                base_url=self._resolve_provider_model_list_base_url(
                    spec,
                    runtime["base_url"],
                    base_url_preset_id=base_url_preset_id,
                ),
                default_headers=self._merge_user_agent_header(
                    runtime.get("default_headers"),
                    user_agent,
                ),
                use_proxy=use_proxy,
            )

        if resolved_model_list_strategy == "bedrock":
            return await self._list_models_from_bedrock(
                api_key=runtime["api_key"],
                base_url=runtime.get("base_url"),
                use_proxy=use_proxy,
            )

        if resolved_model_list_strategy == "anthropic_compatible":
            return await self._list_models_from_models_dev_only(
                provider_id=provider_id,
                transport="anthropic",
                base_url=base_url,
                base_url_preset_id=base_url_preset_id,
                use_proxy=use_proxy,
            )

        if resolved_model_list_strategy == "models_dev_only":
            return await self._list_models_from_models_dev_only(
                provider_id=provider_id,
                transport="openai",
                base_url=base_url,
                base_url_preset_id=base_url_preset_id,
                use_proxy=use_proxy,
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
            default_headers=self._merge_user_agent_header(
                runtime.get("default_headers"),
                user_agent,
            ),
            use_proxy=use_proxy,
        )
