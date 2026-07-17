"""Amazon Bedrock provider 的凭证解析、Region 提取与运行时解析测试"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from app.agent.llm.provider import (
    LLMProviderAuthError,
    LLMProviderManager,
)


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    """每个用例前后清理 LLMProviderManager 单例，避免缓存互相污染"""
    LLMProviderManager._instances.clear()
    yield
    LLMProviderManager._instances.clear()


def test_bedrock_provider_registered():
    manager = LLMProviderManager()
    spec = manager.get_provider("amazon-bedrock")

    assert spec.runtime == "bedrock"
    assert spec.model_list_strategy == "bedrock"
    assert spec.base_url_editable is True
    assert spec.default_base_url == "https://bedrock-runtime.us-east-1.amazonaws.com"
    preset_ids = {preset.id for preset in spec.base_url_presets}
    assert "bedrock-us-east-1" in preset_ids
    assert "bedrock-ap-northeast-1" in preset_ids


def test_parse_bedrock_credentials_bearer_api_key():
    credentials = LLMProviderManager._parse_bedrock_credentials(
        "bedrock-api-key-abcdef123456"
    )

    assert credentials["auth_scheme"] == "bearer"
    assert credentials["bearer_token"] == "bedrock-api-key-abcdef123456"


def test_parse_bedrock_credentials_sigv4_ak_sk():
    credentials = LLMProviderManager._parse_bedrock_credentials(
        "AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )

    assert credentials["auth_scheme"] == "sigv4"
    assert credentials["access_key_id"] == "AKIAIOSFODNN7EXAMPLE"
    assert credentials["secret_access_key"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert "session_token" not in credentials


def test_parse_bedrock_credentials_sigv4_with_session_token():
    credentials = LLMProviderManager._parse_bedrock_credentials(
        "ASIAIOSFODNN7EXAMPLE:secret/key:session-token-value"
    )

    assert credentials["auth_scheme"] == "sigv4"
    assert credentials["session_token"] == "session-token-value"


def test_parse_bedrock_credentials_empty_rejected():
    with pytest.raises(LLMProviderAuthError):
        LLMProviderManager._parse_bedrock_credentials("")


def test_parse_bedrock_credentials_malformed_colon_rejected():
    with pytest.raises(LLMProviderAuthError):
        LLMProviderManager._parse_bedrock_credentials("AKIA123:")


def test_extract_bedrock_region_from_base_url():
    """应从标准、FIPS 与 PrivateLink Bedrock 端点提取 Region"""
    extract = LLMProviderManager._extract_bedrock_region

    assert extract("https://bedrock-runtime.us-east-1.amazonaws.com") == "us-east-1"
    assert extract("https://bedrock-runtime.ap-northeast-1.amazonaws.com/") == "ap-northeast-1"
    assert extract("https://bedrock-runtime.mx-central-1.amazonaws.com") == "mx-central-1"
    assert extract("https://bedrock.eu-central-1.amazonaws.com") == "eu-central-1"
    # FIPS 与 PrivateLink（VPCE）端点同样能识别 Region
    assert extract("https://bedrock-runtime-fips.us-east-1.amazonaws.com") == "us-east-1"
    assert (
        extract("https://vpce-0abc123-xyz.bedrock-runtime.us-west-2.vpce.amazonaws.com")
        == "us-west-2"
    )
    # 无法识别时回退默认 Region
    assert extract("https://example.com/us-west-2") == "us-east-1"
    assert extract("https://example.com?region=.us-west-2.") == "us-east-1"
    assert extract("https://example.com") == "us-east-1"
    assert extract(None) == "us-east-1"
    assert extract("") == "us-east-1"


def test_bedrock_endpoint_url_passthrough():
    """自定义 Bedrock 端点应透传，标准端点交由 boto3 推导"""
    resolve = LLMProviderManager._bedrock_endpoint_url

    # 标准公有端点交由 boto3 推导，不显式透传
    assert resolve("bedrock-runtime", "https://bedrock-runtime.us-east-1.amazonaws.com") is None
    assert resolve("bedrock", "https://bedrock.eu-central-1.amazonaws.com") is None
    assert resolve("bedrock-runtime", None) is None
    assert resolve("bedrock-runtime", "") is None
    # FIPS / PrivateLink 等非标准端点需要显式生效
    assert (
        resolve("bedrock-runtime", "https://bedrock-runtime-fips.us-east-1.amazonaws.com")
        == "https://bedrock-runtime-fips.us-east-1.amazonaws.com"
    )
    assert (
        resolve(
            "bedrock-runtime",
            "https://vpce-0abc123-xyz.bedrock-runtime.us-west-2.vpce.amazonaws.com/",
        )
        == "https://vpce-0abc123-xyz.bedrock-runtime.us-west-2.vpce.amazonaws.com"
    )
    # runtime 端点填给控制面服务名时不匹配标准形态，同样透传
    assert (
        resolve("bedrock", "https://bedrock-runtime.us-east-1.amazonaws.com")
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
    )


def test_create_bedrock_client_uses_custom_endpoint():
    """创建 Bedrock 客户端时应把 PrivateLink 地址传给 boto3"""
    manager = LLMProviderManager()
    endpoint_url = (
        "https://vpce-0abc123-xyz.bedrock-runtime.us-west-2.vpce.amazonaws.com"
    )
    client = MagicMock()

    with patch("boto3.client", return_value=client) as create_client:
        result = manager.create_bedrock_client(
            service_name="bedrock-runtime",
            region="us-west-2",
            credentials={
                "auth_scheme": "sigv4",
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "secret",
            },
            base_url=endpoint_url,
            use_proxy=False,
        )

    assert result is client
    assert create_client.call_args.kwargs["endpoint_url"] == endpoint_url


def test_resolve_runtime_bedrock_bearer():
    manager = LLMProviderManager()
    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="amazon-bedrock",
            model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            api_key="bedrock-api-key-abc123",
            base_url="https://bedrock-runtime.ap-northeast-1.amazonaws.com",
        )
    )

    assert runtime["runtime"] == "bedrock"
    assert runtime["aws_region"] == "ap-northeast-1"
    assert runtime["aws_auth"]["auth_scheme"] == "bearer"


def test_resolve_runtime_bedrock_sigv4_default_region():
    manager = LLMProviderManager()
    runtime = asyncio.run(
        manager.resolve_runtime(
            provider_id="amazon-bedrock",
            model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            api_key="AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG",
        )
    )

    assert runtime["runtime"] == "bedrock"
    assert runtime["aws_region"] == "us-east-1"
    assert runtime["aws_auth"]["auth_scheme"] == "sigv4"
    assert runtime["aws_auth"]["access_key_id"] == "AKIAIOSFODNN7EXAMPLE"


def test_resolve_runtime_bedrock_missing_credentials_rejected():
    manager = LLMProviderManager()
    with pytest.raises(LLMProviderAuthError):
        asyncio.run(
            manager.resolve_runtime(
                provider_id="amazon-bedrock",
                model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                api_key=None,
            )
        )


def test_bedrock_model_matches_region():
    """目录模型应按 Profile 分区及裸模型 ON_DEMAND Region 过滤"""
    matches = LLMProviderManager._bedrock_model_matches_region

    # 已知裸模型 ID 仅在其支持 ON_DEMAND 的 Region 保留
    assert matches("anthropic.claude-3-5-sonnet-20241022-v2:0", "us-west-2")
    assert matches("anthropic.claude-3-5-sonnet-20241022-v2:0", "ap-southeast-2")
    assert not matches("anthropic.claude-3-5-sonnet-20241022-v2:0", "ap-northeast-1")
    assert not matches("anthropic.claude-sonnet-4-5-20250929-v1:0", "us-west-2")
    assert not matches("amazon.nova-premier-v1:0", "ap-northeast-1")
    assert not matches("meta.llama4-maverick-17b-instruct-v1:0", "ap-northeast-1")
    # 已确认支持 ON_DEMAND 的裸模型与 global Profile 维持可用
    assert matches("amazon.nova-lite-v1:0", "ap-northeast-1")
    assert matches("openai.gpt-oss-20b-1:0", "ap-northeast-1")
    assert not matches("openai.gpt-oss-20b-1:0", "ap-southeast-1")
    assert matches("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "ap-northeast-1")
    assert not matches(
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us-gov-west-1",
    )
    # 地理前缀只在对应分区 Region 可调用
    assert matches("us.anthropic.claude-haiku-4-5-20251001-v1:0", "us-west-2")
    assert not matches("us.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-northeast-1")
    assert not matches("us.anthropic.claude-haiku-4-5-20251001-v1:0", "us-gov-west-1")
    assert matches("apac.amazon.nova-micro-v1:0", "ap-southeast-1")
    assert not matches("apac.amazon.nova-micro-v1:0", "eu-central-1")
    assert matches("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-central-1")
    assert not matches("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "us-east-1")
    assert not matches("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-isoe-west-1")


def test_bedrock_au_profile_matches_melbourne_region():
    """AU Inference Profile 应允许从悉尼和墨尔本 Region 调用"""
    matches = LLMProviderManager._bedrock_model_matches_region

    assert matches("au.amazon.nova-lite-v1:0", "ap-southeast-2")
    assert matches("au.amazon.nova-lite-v1:0", "ap-southeast-4")


def test_list_models_bedrock_custom_endpoint_skips_control_plane():
    """自定义 runtime 端点刷新模型时应直接使用离线目录"""
    manager = LLMProviderManager()
    manager._models_dev_data = {
        "amazon-bedrock": {
            "id": "amazon-bedrock",
            "name": "Amazon Bedrock",
            "models": {
                "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "name": "Claude Sonnet 4.5 (Global)",
                    "limit": {"context": 200000, "output": 64000},
                },
            },
        }
    }
    manager._models_dev_loaded_at = time.time()

    with patch.object(
        LLMProviderManager,
        "create_bedrock_client",
        side_effect=AssertionError("不应访问控制面"),
    ):
        models = asyncio.run(
            manager._list_models_from_bedrock(
                api_key="bedrock-api-key-runtime-only",
                base_url="https://bedrock-runtime-fips.us-east-1.amazonaws.com",
                use_proxy=False,
            )
        )

    assert [model["id"] for model in models] == [
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    ]


def test_list_models_bedrock_keeps_control_plane_on_demand_models():
    """控制面返回的 ON_DEMAND 基础模型不应被静态降级规则遗漏"""
    manager = LLMProviderManager()
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": (
                        "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
                    ),
                    "inferenceProfileName": "Claude Sonnet 4.5 (Global)",
                    "status": "ACTIVE",
                }
            ]
        }
    ]
    client.list_foundation_models.return_value = {
        "modelSummaries": [
            {
                "modelId": "openai.gpt-oss-20b-1:0",
                "modelName": "GPT OSS 20B",
                "modelLifecycle": {"status": "ACTIVE"},
            },
            {
                "modelId": "amazon.nova-lite-v1:0",
                "modelName": "Nova Lite",
                "modelLifecycle": {"status": "ACTIVE"},
            },
        ]
    }

    with patch.object(
        LLMProviderManager, "create_bedrock_client", return_value=client
    ):
        models = asyncio.run(
            manager._list_models_from_bedrock(
                api_key="bedrock-api-key-runtime-only",
                base_url="https://bedrock-runtime.ap-northeast-1.amazonaws.com",
                use_proxy=False,
            )
        )

    assert {model["id"] for model in models} == {
        "amazon.nova-lite-v1:0",
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "openai.gpt-oss-20b-1:0",
    }
    client.close.assert_called_once()


def test_list_models_bedrock_falls_back_to_models_dev_on_control_plane_denial():
    """控制面被拒（如 API Key 仅授权 bedrock-runtime）时降级 models.dev 目录"""
    manager = LLMProviderManager()
    # 预填 models.dev 内存缓存，降级路径不触发真实网络请求
    manager._models_dev_data = {
        "amazon-bedrock": {
            "id": "amazon-bedrock",
            "name": "Amazon Bedrock",
            "models": {
                "anthropic.claude-3-5-sonnet-20241022-v2:0": {
                    "name": "Claude Sonnet 3.5 v2",
                    "limit": {"context": 200000, "output": 8192},
                },
                "amazon.nova-lite-v1:0": {
                    "name": "Nova Lite",
                    "limit": {"context": 300000, "output": 5000},
                },
                "openai.gpt-oss-20b-1:0": {
                    "name": "GPT OSS 20B",
                    "limit": {"context": 131072, "output": 16384},
                },
                "apac.amazon.nova-lite-v1:0": {
                    "name": "Nova Lite (APAC)",
                    "limit": {"context": 300000, "output": 5000},
                },
                "meta.llama4-maverick-17b-instruct-v1:0": {
                    "name": "Llama 4 Maverick",
                    "limit": {"context": 1000000, "output": 8192},
                },
                "anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "name": "Claude Sonnet 4.5",
                    "limit": {"context": 200000, "output": 64000},
                },
                "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "name": "Claude Sonnet 4.5 (Global)",
                    "limit": {"context": 200000, "output": 64000},
                },
                "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
                    "name": "Claude Haiku 4.5 (US)",
                    "limit": {"context": 200000, "output": 64000},
                },
            },
        }
    }
    manager._models_dev_loaded_at = time.time()

    denied_client = MagicMock()
    denied_client.get_paginator.side_effect = Exception(
        "AccessDeniedException: not authorized to perform bedrock:ListInferenceProfiles"
    )

    with patch.object(
        LLMProviderManager, "create_bedrock_client", return_value=denied_client
    ):
        models = asyncio.run(
            manager._list_models_from_bedrock(
                api_key="bedrock-api-key-runtime-only",
                base_url="https://bedrock-runtime.ap-northeast-1.amazonaws.com",
                use_proxy=False,
            )
        )

    # 降级后仅保留东京 Region 可调用的裸模型与 Profile
    model_ids = {m["id"] for m in models}
    assert "anthropic.claude-3-5-sonnet-20241022-v2:0" not in model_ids
    assert "amazon.nova-lite-v1:0" in model_ids
    assert "openai.gpt-oss-20b-1:0" in model_ids
    assert "apac.amazon.nova-lite-v1:0" in model_ids
    assert "meta.llama4-maverick-17b-instruct-v1:0" not in model_ids
    assert "global.anthropic.claude-sonnet-4-5-20250929-v1:0" in model_ids
    assert "us.anthropic.claude-haiku-4-5-20251001-v1:0" not in model_ids
    assert "anthropic.claude-sonnet-4-5-20250929-v1:0" not in model_ids
    assert all(m["source"] == "models.dev" for m in models)
    denied_client.close.assert_called_once()
