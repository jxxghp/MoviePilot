"""LLM provider 管理动作依赖的操作端口注入契约测试。

provider.py 不静态依赖 helper.py：测试连接与模型目录查询两个管理动作必须通过
configure_llm_operations 注入的端口取得构建能力；未注入时须给出明确错误而不是
在 None 上崩溃，已注入时须把调用正确委托给注入的实现。
"""
import asyncio
from unittest.mock import patch

import pytest

from app.agent.llm import provider as provider_module
from app.agent.llm.provider import LLMProviderManager, configure_llm_operations
from app.runtime.config import settings


@pytest.fixture
def manager():
    return LLMProviderManager()


class _FakeLLMOperations:
    """满足 _LLMOperationsPort 协议的测试替身，记录调用参数供断言。"""

    def __init__(self, test_result=None, models_result=None):
        self.test_calls = []
        self.get_models_calls = []
        self._test_result = test_result if test_result is not None else {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "reply_preview": "OK",
        }
        self._models_result = models_result if models_result is not None else [{"id": "gpt-4o"}]

    async def test_current_settings(self, **kwargs):
        """记录调用参数并返回预设结果。"""
        self.test_calls.append(kwargs)
        return self._test_result

    async def get_models(self, provider, **kwargs):
        """记录调用参数并返回预设模型列表。"""
        self.get_models_calls.append((provider, kwargs))
        return self._models_result


def test_require_llm_operations_raises_clear_runtime_error_when_unconfigured(monkeypatch):
    """未注入时取用函数须抛出可读的 RuntimeError，而不是留给调用方踩 None。"""
    monkeypatch.setattr(provider_module, "_llm_operations", None)

    with pytest.raises(RuntimeError, match="configure_llm_operations"):
        provider_module._require_llm_operations()


def test_manage_test_without_injection_returns_clear_error_not_none_crash(manager, monkeypatch):
    """未注入时测试连接动作返回明确错误，不能崩在 NoneType 属性访问上。"""
    monkeypatch.setattr(provider_module, "_llm_operations", None)
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        settings, "LLM_MODEL", "deepseek-chat"
    ):
        result = asyncio.run(
            manager.provider_manage("deepseek", "test", model="deepseek-chat", api_key="sk-test")
        )

    assert result["success"] is False
    assert "NoneType" not in result["message"]
    assert "configure_llm_operations" in result["message"]


def test_manage_list_models_without_injection_returns_clear_error_not_none_crash(
    manager, monkeypatch
):
    """未注入时模型目录查询动作返回明确错误，不能崩在 NoneType 属性访问上。"""
    monkeypatch.setattr(provider_module, "_llm_operations", None)

    result = asyncio.run(manager.provider_manage("openai", "list_models"))

    assert result["success"] is False
    assert "NoneType" not in result["message"]
    assert "configure_llm_operations" in result["message"]


def test_manage_test_delegates_to_injected_operations(manager, monkeypatch):
    """注入满足协议的替身后，测试连接动作应委托给该替身并透传结果。"""
    fake = _FakeLLMOperations()
    configure_llm_operations(fake)
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)

    with patch.object(settings, "AI_AGENT_ENABLE", True):
        result = asyncio.run(
            manager.provider_manage(
                "deepseek", "test", model="deepseek-chat", api_key="sk-test"
            )
        )

    assert result["success"] is True
    assert result["data"]["reply_preview"] == "OK"
    assert len(fake.test_calls) == 1
    assert fake.test_calls[0]["provider"] == "deepseek"


def test_manage_list_models_delegates_to_injected_operations(manager, monkeypatch):
    """注入满足协议的替身后，模型目录查询动作应委托给该替身并透传结果。"""
    fake = _FakeLLMOperations(models_result=[{"id": "gpt-4o"}])
    configure_llm_operations(fake)
    monkeypatch.setattr(manager, "get_auth_status", lambda provider_id: {"connected": False})

    result = asyncio.run(manager.provider_manage("openai", "list_models", api_key="sk-test"))

    assert result["success"] is True
    assert result["data"]["models"] == [{"id": "gpt-4o"}]
    assert fake.get_models_calls[0][0] == "openai"
