"""
LLM 提供商通用管理契约（provider_manage）守护测试

验证通用模式的三条核心性质：
1. 动作词汇表由 schemas 契约层统一定义，未支持动作返回统一错误结构
2. 动作标识兼容枚举与原始字符串，统一返回 {"success", "message", "data"}
3. 端点层零提供商特色：ManageRequest 原样透传，默认值填充/校验/脱敏封闭在 Manager 内
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import schemas
from app.agent.llm.gateway import register_llm_provider_runtime
from app.agent.llm.helper import LLMHelper
from app.agent.llm.provider import LLMProviderManager
from app.runtime.config import settings
from app.schemas.types import LlmProviderAction


@pytest.fixture
def manager():
    return LLMProviderManager()


def test_provider_manage_rejects_unknown_action(manager):
    """动作词汇表之外的请求返回统一错误结构。"""
    result = asyncio.run(manager.provider_manage("openai", "not_an_action"))
    assert result["success"] is False
    assert "不支持" in result["message"]
    assert "data" in result


def test_provider_manage_accepts_enum_and_string_action(manager, monkeypatch):
    """动作标识兼容枚举对象与原始字符串，两种形式等价。"""
    clear_mock = AsyncMock()
    monkeypatch.setattr(manager, "clear_auth", clear_mock)
    for action in (LlmProviderAction.DISCONNECT, "disconnect"):
        result = asyncio.run(manager.provider_manage("openai", action))
        assert result["success"] is True
        assert result["data"] is None
    assert clear_mock.await_count == 2


def test_provider_manage_test_requires_ai_agent_enabled(manager):
    """智能助手未启用时测试动作返回提示。"""
    with patch.object(settings, "AI_AGENT_ENABLE", False):
        result = asyncio.run(manager.provider_manage("deepseek", "test"))
    assert result["success"] is False
    assert result["message"] == "请先启用智能助手"


def test_provider_manage_test_requires_model(manager):
    """未配置模型时测试动作返回提示。"""
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        settings, "LLM_API_KEY", "sk-test"
    ), patch.object(settings, "LLM_MODEL", ""):
        result = asyncio.run(manager.provider_manage("deepseek", "test"))
    assert result["success"] is False
    assert result["message"] == "请先配置 LLM 模型"


def test_provider_manage_test_requires_api_key(manager, monkeypatch):
    """无 OAuth 授权方式且无已保存凭据的提供商必须配置 API Key。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        settings, "LLM_API_KEY", None
    ), patch.object(settings, "LLM_MODEL", "deepseek-chat"):
        result = asyncio.run(manager.provider_manage("deepseek", "test"))
    assert result["success"] is False
    assert result["message"] == "请先配置 LLM API Key"
    assert result["data"]["model"] == "deepseek-chat"


def test_provider_manage_test_exempts_oauth_providers_from_api_key(manager, monkeypatch):
    """支持 OAuth 授权的提供商无需 API Key，端点与 Manager 均不硬编码提供商名。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    monkeypatch.setattr(
        manager, "get_provider", lambda provider_id: SimpleNamespace(oauth_methods=("browser_oauth",))
    )
    test_mock = AsyncMock(return_value={"provider": "chatgpt", "model": "gpt-4o", "reply_preview": "OK"})
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        settings, "LLM_API_KEY", None
    ), patch.object(LLMHelper, "test_current_settings", test_mock):
        result = asyncio.run(
            manager.provider_manage("chatgpt", "test", model="gpt-4o")
        )
    assert result["success"] is True
    test_mock.assert_awaited_once()


def test_provider_manage_test_returns_reply_preview(manager, monkeypatch):
    """测试成功时返回模型响应预览，显式参数优先于已保存配置。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    test_mock = AsyncMock(
        return_value={"provider": "openai", "model": "gpt-4.1-mini", "duration_ms": 123, "reply_preview": "OK"}
    )
    with patch.object(settings, "AI_AGENT_ENABLE", False), patch.object(
        LLMHelper, "test_current_settings", test_mock
    ):
        result = asyncio.run(
            manager.provider_manage(
                "openai",
                "test",
                enabled=True,
                model="gpt-4.1-mini",
                thinking_level="high",
                api_key="sk-live",
                base_url="https://example.com/v1",
                use_proxy=False,
            )
        )
    test_mock.assert_awaited_once_with(
        provider="openai",
        model="gpt-4.1-mini",
        thinking_level="high",
        api_key="sk-live",
        base_url="https://example.com/v1",
        base_url_preset=None,
        user_agent=None,
        use_proxy=False,
        api_protocol=None,
        web_search_mode=None,
    )
    assert result["success"] is True
    assert result["data"]["reply_preview"] == "OK"


def test_provider_manage_test_rejects_empty_reply(manager, monkeypatch):
    """模型响应为空时返回失败但保留结果详情。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        LLMHelper,
        "test_current_settings",
        AsyncMock(return_value={"provider": "deepseek", "model": "deepseek-chat", "duration_ms": 12}),
    ):
        result = asyncio.run(
            manager.provider_manage("deepseek", "test", model="deepseek-chat", api_key="sk-test")
        )
    assert result["success"] is False
    assert result["message"] == "模型响应为空"
    assert result["data"]["duration_ms"] == 12


def test_provider_manage_test_maps_timeout_error(manager, monkeypatch):
    """调用超时返回统一的超时提示。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        LLMHelper,
        "test_current_settings",
        AsyncMock(side_effect=TimeoutError("request timed out")),
    ):
        result = asyncio.run(
            manager.provider_manage("deepseek", "test", model="deepseek-chat", api_key="sk-test")
        )
    assert result["success"] is False
    assert result["message"] == "LLM 调用超时"


def test_provider_manage_test_sanitizes_error_message(manager, monkeypatch):
    """错误信息中的密钥与授权头必须脱敏。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    raw_error = (
        "request failed api_key=sk-secret "
        "Authorization: Bearer sk-secret "
        "base error sk-secret"
    )
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        LLMHelper, "test_current_settings", AsyncMock(side_effect=RuntimeError(raw_error))
    ):
        result = asyncio.run(
            manager.provider_manage("deepseek", "test", model="deepseek-chat", api_key="sk-secret")
        )
    assert result["success"] is False
    assert "sk-secret" not in result["message"]
    assert "Authorization: Bearer" not in result["message"]
    assert "***" in result["message"]


def test_provider_manage_test_maps_internal_error_to_base_url_hint(manager, monkeypatch):
    """SDK 内部响应解析错误应改写为可定位的基础地址提示。"""
    monkeypatch.setattr(manager, "get_saved_auth", lambda provider_id: None)
    with patch.object(settings, "AI_AGENT_ENABLE", True), patch.object(
        LLMHelper,
        "test_current_settings",
        AsyncMock(side_effect=RuntimeError("'str' object has no attribute 'model_dump'")),
    ):
        result = asyncio.run(
            manager.provider_manage("openai", "test", model="gpt-4o-mini", api_key="sk-test")
        )
    assert result["success"] is False
    assert "基础地址" in result["message"]
    assert "API Base URL" in result["message"]
    assert "model_dump" not in result["message"]


def test_provider_manage_list_models_sanitizes_base_url_hint(manager):
    """模型列表查询遇到 SDK 内部错误时同样给出基础地址提示。"""
    with patch.object(
        LLMHelper,
        "get_models",
        AsyncMock(side_effect=AttributeError("'str' object has no attribute '_set_private_attributes'")),
    ):
        result = asyncio.run(
            manager.provider_manage("openai", "list_models", api_key="sk-test", base_url="https://example.com")
        )
    assert result["success"] is False
    assert "基础地址" in result["message"]
    assert "API Base URL" in result["message"]
    assert "_set_private_attributes" not in result["message"]


def test_provider_manage_list_models_returns_catalog_with_auth_status(manager):
    """模型目录查询成功时附带授权状态摘要。"""
    models = [{"id": "gpt-4o"}]
    with patch.object(LLMHelper, "get_models", AsyncMock(return_value=models)), patch.object(
        manager, "get_auth_status", lambda provider_id: {"connected": False}
    ):
        result = asyncio.run(manager.provider_manage("openai", "list_models"))
    assert result["success"] is True
    assert result["data"]["provider"] == "openai"
    assert result["data"]["models"] == models
    assert result["data"]["auth_status"] == {"connected": False}


def test_llm_manage_endpoint_passes_through_manage_request(monkeypatch):
    """端点仅透传 ManageRequest，并按具名回调路由注入 OAuth 回跳地址。"""
    from app.api.endpoints import llm as llm_endpoint

    captured = {}

    async def fake_manage(self, provider, action, **params):
        captured["provider"] = provider
        captured["action"] = action
        captured["params"] = params
        return {"success": True, "message": "", "data": {"ok": True}}

    monkeypatch.setattr(LLMProviderManager, "provider_manage", fake_manage)
    request = SimpleNamespace(
        url_for=lambda name, **kwargs: f"https://host/api/v1/llm/provider-auth/callback/{kwargs['provider_id']}"
    )
    payload = schemas.ManageRequest(target="chatgpt", action="start_auth", params={"method": "browser_oauth"})

    resp = asyncio.run(llm_endpoint.manage_provider(request, payload, _="token"))

    assert resp.success is True
    assert resp.data == {"ok": True}
    assert captured["provider"] == "chatgpt"
    assert captured["action"] == "start_auth"
    assert captured["params"]["method"] == "browser_oauth"
    assert captured["params"]["callback_url"].endswith("/callback/chatgpt")


def test_llm_manage_endpoint_accepts_empty_target(monkeypatch):
    """目录类查询动作 target 可为空，不得因 url_for 空路径参数报 500。

    回归守护：前端加载提供商目录时 target 为空字符串，
    回调地址构造必须跳过空 target。
    """
    from app.api.endpoints import llm as llm_endpoint

    captured = {}

    async def fake_manage(self, provider, action, **params):
        captured["provider"] = provider
        captured["params"] = params
        return {"success": True, "message": "", "data": []}

    def fail_url_for(name, **kwargs):
        raise AssertionError("空 target 不应构造回调地址")

    monkeypatch.setattr(LLMProviderManager, "provider_manage", fake_manage)
    request = SimpleNamespace(url_for=fail_url_for)
    payload = schemas.ManageRequest(target="", action="list_providers")

    resp = asyncio.run(llm_endpoint.manage_provider(request, payload, _="token"))

    assert resp.success is True
    assert captured["provider"] == ""
    assert "callback_url" not in captured["params"]


def test_llm_manage_endpoint_uses_registered_provider_runtime():
    """管理端点必须使用组合根登记的 runtime，不得自行构造 provider Singleton。"""
    captured = {}

    class ProviderRuntime:
        """记录端点调用的最小 provider runtime。"""

        async def provider_manage(self, provider, action, **params):
            """记录统一管理参数并返回可识别结果。"""
            captured.update(provider=provider, action=action, params=params)
            return {"success": True, "message": "", "data": {"runtime": "registered"}}

    from app.api.endpoints import llm as llm_endpoint

    previous = register_llm_provider_runtime(ProviderRuntime)
    try:
        request = SimpleNamespace(url_for=lambda *_args, **_kwargs: "unused")
        payload = schemas.ManageRequest(target="", action="list_providers")

        response = asyncio.run(llm_endpoint.manage_provider(request, payload, _="token"))
    finally:
        register_llm_provider_runtime(previous)

    assert response.data == {"runtime": "registered"}
    assert captured == {
        "provider": "",
        "action": "list_providers",
        "params": {},
    }


def test_llm_oauth_callback_uses_registered_provider_runtime():
    """OAuth 回调必须与管理端点复用组合根登记的 provider runtime。"""
    captured = {}

    class ProviderRuntime:
        """记录 OAuth 回调参数的最小 provider runtime。"""

        async def handle_chatgpt_callback(
            self,
            provider_id,
            code,
            state,
            error,
            error_description,
        ):
            """记录回调并返回可渲染的成功结果。"""
            captured.update(
                provider_id=provider_id,
                code=code,
                state=state,
                error=error,
                error_description=error_description,
            )
            return True, "registered runtime"

    from app.api.endpoints import llm as llm_endpoint

    previous = register_llm_provider_runtime(ProviderRuntime)
    try:
        response = asyncio.run(
            llm_endpoint.llm_provider_auth_callback(
                provider_id="chatgpt",
                code="oauth-code",
                state="oauth-state",
            )
        )
    finally:
        register_llm_provider_runtime(previous)

    assert response.status_code == 200
    assert b"registered runtime" in response.body
    assert captured == {
        "provider_id": "chatgpt",
        "code": "oauth-code",
        "state": "oauth-state",
        "error": None,
        "error_description": None,
    }


def test_llm_manage_endpoint_response_model_accepts_list_data():
    """目录查询动作 data 为列表，响应模型须同时覆盖列表与映射形态。

    回归守护：list_providers 返回 list[dict]，
    若响应模型声明为 Dict[str, Any] 会在序列化校验时直接 500；
    同时受响应模型守护测试约束，不得使用 Any/JsonData 弱类型。
    """
    from typing import Any, Dict, List, Union

    from app.api.endpoints import llm as llm_endpoint

    route = next(
        r for r in llm_endpoint.router.routes if getattr(r, "path", "").endswith("/manage")
    )
    assert route.response_model is schemas.Response[
        Union[List[Dict[str, Any]], Dict[str, Any]]
    ]

    list_resp = schemas.Response[Union[List[Dict[str, Any]], Dict[str, Any]]](
        success=True, message="", data=[{"id": "openai", "name": "OpenAI 兼容"}]
    )
    assert list_resp.data == [{"id": "openai", "name": "OpenAI 兼容"}]

    dict_resp = schemas.Response[Union[List[Dict[str, Any]], Dict[str, Any]]](
        success=True, message="", data={"models": ["gpt-4o"]}
    )
    assert dict_resp.data == {"models": ["gpt-4o"]}
