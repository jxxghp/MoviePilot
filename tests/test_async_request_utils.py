import asyncio
import time

import httpx2
import pytest

from app.adapters.network import http as http_module
from app.adapters.network.http import AsyncRequestUtils
from app.sdk.network import AsyncRequestUtils as SdkAsyncRequestUtils

PROXY = "http://proxy.example:7890"
URL = "https://raw.githubusercontent.com/demo/repo/main/package.json"


@pytest.fixture(autouse=True)
def _reset_h2_proxy_breaker():
    """每个用例前后清空熔断状态，避免跨用例污染进程级熔断字典。"""
    http_module._h2_proxy_retry_at.clear()
    yield
    http_module._h2_proxy_retry_at.clear()


def _fake_dispatch(calls, fail_when):
    async def fake(_self, http2, _cookies_dict, _method, _url, raise_exception, **_kwargs):
        calls.append(http2)
        if fail_when(http2):
            if raise_exception:
                raise httpx2.RemoteProtocolError("tunnel closed")
            return None
        return "ok"

    return fake


def test_get_downgrades_to_h1_and_trips_breaker_on_h2_failure(monkeypatch):
    """
    走代理的幂等请求 h2 遇到连接层失败时，就地降级 h1 重试一次并触发该 (proxy, host) 的熔断。
    """
    calls = []
    monkeypatch.setattr(
        AsyncRequestUtils, "_dispatch_request", _fake_dispatch(calls, fail_when=lambda http2: http2)
    )

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    result = asyncio.run(utils.request("get", URL))

    assert result == "ok"
    assert calls == [True, False]
    assert http_module._h2_proxy_allowed(PROXY, URL) is False


def test_get_skips_h2_while_breaker_is_tripped(monkeypatch):
    """
    熔断冷却期内，走代理的请求直接使用 h1，不再尝试 h2。
    """
    http_module._trip_h2_proxy_breaker(PROXY, URL)
    calls = []
    monkeypatch.setattr(
        AsyncRequestUtils, "_dispatch_request", _fake_dispatch(calls, fail_when=lambda http2: False)
    )

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    result = asyncio.run(utils.request("get", URL))

    assert result == "ok"
    assert calls == [False]


def test_get_retries_h2_after_cooldown_expires(monkeypatch):
    """
    熔断冷却期结束后，代理请求恢复尝试 h2。
    """
    http_module._h2_proxy_retry_at[
        http_module._h2_proxy_breaker_key(PROXY, URL)
    ] = time.monotonic() - 1
    calls = []
    monkeypatch.setattr(
        AsyncRequestUtils, "_dispatch_request", _fake_dispatch(calls, fail_when=lambda http2: False)
    )

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    result = asyncio.run(utils.request("get", URL))

    assert result == "ok"
    assert calls == [True]


def test_timeout_does_not_trip_breaker_or_retry(monkeypatch):
    """
    超时等与 h2 隧道无关的错误不触发熔断、不做 h1 重试，且保持 raise_exception=False 返回 None 的语义。
    """
    calls = []

    async def fake(_self, http2, _cookies_dict, _method, _url, _raise_exception, **_kwargs):
        calls.append(http2)
        raise httpx2.ConnectTimeout("proxy slow")

    monkeypatch.setattr(AsyncRequestUtils, "_dispatch_request", fake)

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    result = asyncio.run(utils.request("get", URL))

    assert result is None
    assert calls == [True]
    assert http_module._h2_proxy_allowed(PROXY, URL) is True


def test_timeout_still_raises_when_raise_exception_enabled(monkeypatch):
    """
    与 h2 隧道无关的错误在 raise_exception=True 时按原语义抛出。
    """
    calls = []

    async def fake(_self, http2, _cookies_dict, _method, _url, _raise_exception, **_kwargs):
        calls.append(http2)
        raise httpx2.ConnectTimeout("proxy slow")

    monkeypatch.setattr(AsyncRequestUtils, "_dispatch_request", fake)

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    with pytest.raises(httpx2.ConnectTimeout):
        asyncio.run(utils.request("get", URL, raise_exception=True))

    assert calls == [True]
    assert http_module._h2_proxy_allowed(PROXY, URL) is True


def test_post_does_not_downgrade_on_h2_failure(monkeypatch):
    """
    非幂等方法 h2 失败时不做 h1 降级重试，避免服务端可能已收到数据时重复产生副作用。
    """
    calls = []
    monkeypatch.setattr(
        AsyncRequestUtils, "_dispatch_request", _fake_dispatch(calls, fail_when=lambda http2: True)
    )

    utils = AsyncRequestUtils(proxies={"https": PROXY})
    result = asyncio.run(utils.request("post", URL))

    assert result is None
    assert calls == [True]


def test_no_proxy_configured_skips_breaker_logic(monkeypatch):
    """
    未配置代理时不触发熔断判断，行为与原实现一致（仅走一次 h2）。
    """
    calls = []
    monkeypatch.setattr(
        AsyncRequestUtils, "_dispatch_request", _fake_dispatch(calls, fail_when=lambda http2: False)
    )

    utils = AsyncRequestUtils()
    result = asyncio.run(utils.request("get", URL))

    assert result == "ok"
    assert calls == [True]


def test_internal_client_uses_httpx2_transport(monkeypatch):
    """宿主自建客户端使用 HTTPX2，并返回对应响应对象。"""

    async def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"client": "httpx2"}, request=request)

    monkeypatch.setattr(
        http_module,
        "_get_shared_async_transport",
        lambda **_kwargs: httpx2.MockTransport(respond),
    )

    response = asyncio.run(
        AsyncRequestUtils().get_res("https://example.com/data", raise_exception=True)
    )

    assert isinstance(response, httpx2.Response)
    assert response.json() == {"client": "httpx2"}


def test_plugin_sdk_uses_httpx2_by_default(monkeypatch):
    """插件 SDK 默认复用宿主 HTTPX2 客户端合同。"""

    async def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request)

    monkeypatch.setattr(
        http_module,
        "_get_shared_async_transport",
        lambda **_kwargs: httpx2.MockTransport(respond),
    )

    response = asyncio.run(
        SdkAsyncRequestUtils().get_res(
            "https://example.com/sdk", raise_exception=True
        )
    )

    assert SdkAsyncRequestUtils is AsyncRequestUtils
    assert isinstance(response, httpx2.Response)


def test_legacy_http_module_uses_httpx2_by_default(monkeypatch):
    """旧插件 HTTP 导入入口随宿主默认客户端迁移到 HTTPX2。"""
    import importlib

    legacy_http = importlib.import_module("app.utils.http")

    async def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request)

    monkeypatch.setattr(
        http_module,
        "_get_shared_async_transport",
        lambda **_kwargs: httpx2.MockTransport(respond),
    )

    response = asyncio.run(
        legacy_http.AsyncRequestUtils().get_res(
            "https://example.com/legacy", raise_exception=True
        )
    )

    assert legacy_http.AsyncRequestUtils is AsyncRequestUtils
    assert isinstance(response, httpx2.Response)
