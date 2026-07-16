"""Amazon Bedrock provider 的凭证解析、Region 提取与运行时解析测试"""

import asyncio

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
    extract = LLMProviderManager._extract_bedrock_region

    assert extract("https://bedrock-runtime.us-east-1.amazonaws.com") == "us-east-1"
    assert extract("https://bedrock-runtime.ap-northeast-1.amazonaws.com/") == "ap-northeast-1"
    assert extract("https://bedrock.eu-central-1.amazonaws.com") == "eu-central-1"
    # 无法识别时回退默认 Region
    assert extract("https://example.com") == "us-east-1"
    assert extract(None) == "us-east-1"
    assert extract("") == "us-east-1"


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
