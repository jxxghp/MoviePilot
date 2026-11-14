"""
验证 ChatGPT 轮换令牌的并发续期、持久化和错误归因。
"""

import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest

from app.agent.llm.provider import LLMProviderAuthError, LLMProviderManager


@pytest.fixture
def auth_state(monkeypatch):
    """
    使用独立管理器和内存配置替代持久化边界，禁止访问真实凭据和网络。
    """
    manager = object.__new__(LLMProviderManager)
    manager.__init__()
    config = {
        "provider_auth": {
            "chatgpt": {
                "type": "oauth",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 60,
                "account_id": "test-account",
            },
            "github-copilot": {"type": "oauth", "access_token": "other-provider"},
        },
    }

    async def write_config(value):
        """
        模拟异步落库完成后才发布配置快照。
        """
        await asyncio.sleep(0)
        config.clear()
        config.update(copy.deepcopy(value))

    monkeypatch.setattr(manager, "_read_agent_config", lambda: copy.deepcopy(config))
    monkeypatch.setattr(manager, "_write_agent_config", AsyncMock(side_effect=write_config))
    monkeypatch.setattr(manager, "_jwt_claims", lambda _token: {})
    monkeypatch.setattr(manager, "_build_async_request", lambda **_kwargs: pytest.fail("意外网络请求"))
    return manager, config


def test_concurrent_requests_refresh_and_persist_once(auth_state, monkeypatch):
    """
    并发调用只使用一次旧令牌，所有等待者读取已持久化的新令牌和官方有效期。
    """
    manager, config = auth_state
    now = int(time.time())

    async def refresh(_token):
        """
        模拟网络等待，让未受保护的并发请求同时持有旧令牌。
        """
        await asyncio.sleep(0)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 864000}

    refresh_mock = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(manager, "_refresh_chatgpt_tokens", refresh_mock)

    async def resolve_all():
        """
        同时模拟聊天、识别和模型目录调用。
        """
        return await asyncio.gather(*(manager._resolve_chatgpt_oauth() for _ in range(24)))

    results = asyncio.run(resolve_all())

    refresh_mock.assert_awaited_once_with("old-refresh")
    manager._write_agent_config.assert_awaited_once()
    saved = config["provider_auth"]["chatgpt"]
    assert all(result == saved for result in results)
    assert saved["refresh_token"] == "new-refresh"
    assert now + 864000 <= saved["expires_at"] <= int(time.time()) + 864000
    assert config["provider_auth"]["github-copilot"]["access_token"] == "other-provider"


def test_valid_token_does_not_refresh(auth_state, monkeypatch):
    """
    官方有效期内继续使用保存的授权，不提前触发轮换。
    """
    manager, config = auth_state
    config["provider_auth"]["chatgpt"]["expires_at"] = int(time.time()) + 86400
    refresh = AsyncMock()
    monkeypatch.setattr(manager, "_refresh_chatgpt_tokens", refresh)
    result = asyncio.run(manager._resolve_chatgpt_oauth())
    assert result["access_token"] == "old-access"
    refresh.assert_not_awaited()
    manager._write_agent_config.assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError("offline"), LLMProviderAuthError("official rejection")])
def test_refresh_failure_preserves_saved_auth(auth_state, monkeypatch, error):
    """
    网络失败或官方拒绝不会清除授权、伪造有效期或在本次调用中重发。
    """
    manager, config = auth_state
    before = copy.deepcopy(config)
    refresh = AsyncMock(side_effect=error)
    monkeypatch.setattr(manager, "_refresh_chatgpt_tokens", refresh)
    with pytest.raises(type(error), match=str(error)):
        asyncio.run(manager._resolve_chatgpt_oauth())
    assert config == before
    refresh.assert_awaited_once()
    manager._write_agent_config.assert_not_awaited()
    assert not manager._chatgpt_refresh_lock.locked()


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"error": {"code": "refresh_token_reused", "message": "private-response"}}, "已被使用"),
        (401, {"error": {"code": "refresh_token_expired"}}, "已过期"),
        (401, {"error": {"code": "refresh_token_invalidated"}}, "已被撤销"),
        (400, {"error": "invalid_grant", "error_description": "private-response"}, "已失效"),
        (429, {"error": {"code": "private-response"}}, "HTTP 429"),
        (503, ["private-response"], "HTTP 503"),
        (403, None, "HTTP 403"),
    ],
)
def test_refresh_error_uses_safe_official_reason(auth_state, monkeypatch, status, payload, expected):
    """
    官方拒绝保留可操作原因，网关或限流错误不误报注销，正文不出现在错误中。
    """
    manager, _config = auth_state
    response = httpx2.Response(
        status,
        **({"json": payload} if payload is not None else {"text": "<html>private-response</html>"}),
        request=httpx2.Request("POST", "https://auth.openai.com/oauth/token"),
    )
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(manager, "_build_async_request", lambda **_kwargs: SimpleNamespace(post_res=post))
    with pytest.raises(LLMProviderAuthError, match=expected) as caught:
        asyncio.run(manager._refresh_chatgpt_tokens("test-refresh"))
    assert "private-response" not in str(caught.value)
    assert "test-refresh" not in str(caught.value)
    if status in (403, 429, 503):
        assert "重新授权" not in str(caught.value)
    post.assert_awaited_once()


@pytest.mark.parametrize("api_key", [None, "fallback-key"])
def test_runtime_preserves_oauth_failure_without_api_key(auth_state, monkeypatch, api_key):
    """
    只有明确提供 API Key 才回退，否则将真实续期失败传给管理接口。
    """
    manager, _config = auth_state
    provider = manager.get_provider("chatgpt")
    monkeypatch.setattr(manager, "_get_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(manager, "_resolve_chatgpt_oauth", AsyncMock(side_effect=LLMProviderAuthError("已被使用")))
    if api_key:
        result = asyncio.run(manager.resolve_runtime("chatgpt", "test-model", api_key=api_key))
        assert result["auth_mode"] == "api_key"
        assert result["api_key"] == api_key
    else:
        result = asyncio.run(manager.provider_manage("chatgpt", "list_models"))
        assert result["success"] is False
        assert "已被使用" in result["message"]
