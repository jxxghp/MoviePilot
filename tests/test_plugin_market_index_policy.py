"""
插件市场索引同步与异步策略一致性测试。
"""

import asyncio
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external.plugin import client as plugin_client_module
from app.adapters.external.plugin.client import (
    PLUGIN_INDEX_MAX_BYTES,
    PLUGIN_INDEX_MAX_ENTRIES,
    PLUGIN_INDEX_REQUEST_TIMEOUT,
    PluginMarketClient,
    PluginMarketTransport,
    _format_request_error,
)
from app.domain.plugin import split_plugin_market_repo_urls
from app.runtime import cache as cache_module
from app.runtime.cache import fresh
from app.runtime.tasks import TaskRegistry

SYNC_INDEX_REQUEST = "_PluginMarketTransport__request_plugin_index_with_fallback"
ASYNC_INDEX_REQUEST = "_PluginMarketTransport__async_request_plugin_index_with_fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure_status", [None, 404, 503])
async def test_failed_indexes_retry_after_short_cache_and_preserve_success_ttl(monkeypatch, mode, failure_status):
    """
    连接失败与缺失索引五分钟后重试，恢复后的成功索引继续保留三十分钟。
    """
    clock = [0.0]
    original_init = cache_module.MemoryTLRUCache.__init__

    def initialize_cache(self, *args, **kwargs):
        """
        将真实缓存的计时源替换为可前进的测试时钟。
        """
        original_init(self, *args, **kwargs, timer=lambda: clock[0])

    monkeypatch.setattr(cache_module.MemoryTLRUCache, "__init__", initialize_cache)
    monkeypatch.setattr(cache_module.MemoryBackend, "_region_caches", {})
    helper = PluginMarketTransport()
    repo = f"https://github.com/short-cache/{mode}-{failure_status}"
    requests = []

    def request(*_args, **_kwargs):
        """
        冷却期内返回故障，过期后模拟仓库恢复。
        """
        requests.append(clock[0])
        if clock[0] < 300:
            return helper._resolve_plugin_index_result(
                None if failure_status is None else (failure_status, "")
            )
        return {"Demo": {"version": "1.0.0"}}

    async def async_request(*args, **kwargs):
        """
        让异步传输使用相同故障与恢复序列。
        """
        return request(*args, **kwargs)

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    monkeypatch.setattr(helper, ASYNC_INDEX_REQUEST, async_request)

    async def read():
        """
        通过所选真实入口读取仓库索引，保留原始三态合同。
        """
        if mode == "async":
            return await helper.async_get_plugin_index_result(repo, "v3")
        return helper.get_plugin_index_result(repo, "v3")

    for now in (0, 1, 299):
        clock[0] = now
        if failure_status == 404:
            assert await read() is None
        else:
            with pytest.raises(RuntimeError, match="插件索引请求失败"):
                await read()
    assert requests == [0]

    clock[0] = 301
    assert await read() == {"Demo": {"version": "1.0.0"}}
    clock[0] = 602
    assert await read() == {"Demo": {"version": "1.0.0"}}
    assert requests == [0, 301]


@pytest.mark.asyncio
async def test_force_refresh_bypasses_shared_negative_cache(monkeypatch):
    """
    同步和异步共用故障冷却，强制刷新可立即重试并发布恢复结果。
    """
    helper = PluginMarketTransport()
    repo = "https://github.com/short-cache/force-recovery"
    def sync_request(*_args, **_kwargs):
        """
        模拟同步入口首次连接仓库失败。
        """
        return helper._resolve_plugin_index_result(None)

    async def async_request(*_args, **_kwargs):
        """
        模拟强制重试时仓库已经恢复。
        """
        return {"Demo": {"version": "2.0.0"}}

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, sync_request)
    request = AsyncMock(side_effect=async_request)
    monkeypatch.setattr(helper, ASYNC_INDEX_REQUEST, request)
    with pytest.raises(RuntimeError):
        helper.get_plugin_index_result(repo, "v3")
    with pytest.raises(RuntimeError):
        await helper.async_get_plugin_index_result(repo, "v3")
    request.assert_not_awaited()
    with fresh():
        assert await helper.async_get_plugin_index_result(repo, "v3") == {"Demo": {"version": "2.0.0"}}
    assert helper.get_plugin_index_result(repo, "v3") == {"Demo": {"version": "2.0.0"}}
    request.assert_awaited_once()


def test_plugin_index_request_timeout_is_bounded() -> None:
    """
    插件索引故障应在有限时间内返回，不能沿用一分钟等待。
    """
    assert PLUGIN_INDEX_REQUEST_TIMEOUT == 15


def test_plugin_market_repo_config_is_normalized_once() -> None:
    """
    市场配置的空白、尾斜杠和重复仓库不能制造重复读取任务。
    """
    assert split_plugin_market_repo_urls(
        " https://github.com/Example/Plugins.git/ ,"
        "https://github.com/example/plugins,"
        "https://github.com/other/plugins/ "
    ) == [
        "https://github.com/Example/Plugins",
        "https://github.com/other/plugins",
    ]


def test_github_request_strategies_do_not_retry_direct_when_proxy_is_configured(
    monkeypatch,
) -> None:
    """
    显式代理失败后不得绕过同一出口策略再等待一次直连超时。
    """
    settings = {
        "GITHUB_PROXY": None,
        "PROXY_HOST": "http://proxy:7891",
        "PROXY": {"http": "http://proxy:7891", "https": "http://proxy:7891"},
    }
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.get_runtime_setting",
        lambda key: settings.get(key),
    )

    strategies = PluginMarketTransport._build_github_request_strategies(
        url="https://raw.githubusercontent.com/example/repo/main/package.v3.json",
    )

    assert [name for name, _url, _params in strategies] == ["代理"]


def test_github_request_strategies_use_direct_without_configured_proxy(
    monkeypatch,
) -> None:
    """
    未配置代理时仍保留直连出口。
    """
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.get_runtime_setting",
        lambda _key: None,
    )

    strategies = PluginMarketTransport._build_github_request_strategies(
        url="https://raw.githubusercontent.com/example/repo/main/package.v3.json",
    )

    assert [name for name, _url, _params in strategies] == ["直连"]


def test_request_error_formatter_keeps_type_for_blank_exception() -> None:
    """
    无消息异常也必须给运行日志留下可诊断类型。
    """
    assert _format_request_error(TimeoutError()) == "TimeoutError"


class _SyncStreamResponse:
    """
    提供 requests 流式响应所需的最小测试契约。
    """

    def __init__(
        self,
        content: bytes,
        content_length: str | None = None,
        status_code: int = 200,
    ) -> None:
        """
        保存本次离线流的正文、可选长度和 HTTP 状态。

        :param content: 按字节读取的响应正文
        :param content_length: 可选的 Content-Length 头值
        :param status_code: 模拟响应状态码
        """
        self.status_code = status_code
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self._content = content

    def iter_content(self, chunk_size: int):
        """
        按请求的字节块大小迭代索引内容，模拟同步流式响应。

        :param chunk_size: 每个内容块的最大字节数
        :yields: 本次读取的索引字节块
        """
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]


class _AsyncStreamResponse:
    """
    提供 httpx 流式响应所需的最小测试契约。
    """

    def __init__(
        self,
        content: bytes,
        content_length: str | None = None,
        status_code: int = 200,
    ) -> None:
        """
        保存本次离线异步流的正文、可选长度和 HTTP 状态。

        :param content: 按字节读取的响应正文
        :param content_length: 可选的 Content-Length 头值
        :param status_code: 模拟响应状态码
        """
        self.status_code = status_code
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self._content = content

    async def aiter_bytes(self, chunk_size: int):
        """
        按请求的字节块大小异步迭代索引内容，模拟异步流式响应。

        :param chunk_size: 每个内容块的最大字节数
        :yields: 本次读取的索引字节块
        """
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]


def test_plugin_index_request_uses_sync_stream_transport(monkeypatch) -> None:
    """
    同步索引专用请求必须进入流式传输，不能预先加载完整响应体。
    """
    calls: list[tuple[dict, str, bool]] = []

    class FakeRequestUtils:
        """
        记录同步索引请求并返回固定流响应，避免真实网络调用。
        """
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        @contextmanager
        def get_stream(self, url: str, raise_exception: bool):
            """
            记录同步索引请求的地址与异常策略，返回固定索引流。
            """
            calls.append((self._kwargs, url, raise_exception))
            yield _SyncStreamResponse(b'{"DemoPlugin":{"version":"1.0.0"}}')

    monkeypatch.setattr(
        "app.adapters.external.plugin.client.RequestUtils",
        FakeRequestUtils,
    )

    result = PluginMarketTransport._PluginMarketTransport__request_plugin_index_with_fallback(
        "https://raw.githubusercontent.com/example/repo/main/package.v3.json",
        headers={"Accept": "application/json"},
    )

    assert result == {"DemoPlugin": {"version": "1.0.0"}}
    assert calls[0][0]["headers"] == {"Accept": "application/json"}
    assert calls[0][2] is True


@pytest.mark.asyncio
async def test_plugin_index_request_uses_async_stream_transport(monkeypatch) -> None:
    """
    异步索引专用请求与同步入口使用相同的流式资源边界。
    """
    calls: list[tuple[dict, str, bool]] = []

    class FakeAsyncRequestUtils:
        """
        记录异步索引请求并返回固定流响应，避免真实网络调用。
        """
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        @asynccontextmanager
        async def get_stream(self, url: str, raise_exception: bool):
            """
            记录异步索引请求的地址与异常策略，返回固定索引流。
            """
            calls.append((self._kwargs, url, raise_exception))
            yield _AsyncStreamResponse(b'{"DemoPlugin":{"version":"1.0.0"}}')

    monkeypatch.setattr(
        "app.adapters.external.plugin.client.AsyncRequestUtils",
        FakeAsyncRequestUtils,
    )

    result = await PluginMarketTransport._PluginMarketTransport__async_request_plugin_index_with_fallback(
        "https://raw.githubusercontent.com/example/repo/main/package.v3.json",
        headers={"Accept": "application/json"},
    )

    assert result == {"DemoPlugin": {"version": "1.0.0"}}
    assert calls[0][0]["headers"] == {"Accept": "application/json"}
    assert calls[0][2] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    ("mirror", "origin_status", "expected_error"),
    [
        pytest.param((200, b"{}"), 200, None, id="valid-empty-mirror"),
        pytest.param((404, b"<html>not found</html>"), 200, None, id="mirror-404"),
        pytest.param((503, b"unavailable"), 200, None, id="mirror-503"),
        pytest.param((200, b"<html>anti-bot page</html>"), 200, None, id="mirror-html"),
        pytest.param((200, b"[]"), 200, None, id="mirror-json-array"),
        pytest.param((200, b"x" * (PLUGIN_INDEX_MAX_BYTES + 1)), 200, None, id="mirror-oversize"),
        pytest.param((404, b"not found"), 404, None, id="origin-404"),
        pytest.param((404, b"not found"), 503, "HTTP 503", id="origin-503"),
        pytest.param((404, b"not found"), None, "连接失败", id="origin-unreachable"),
    ],
)
async def test_plugin_index_fallback_checks_response_before_accepting_it(
    monkeypatch,
    mode: str,
    mirror: tuple[int, bytes],
    origin_status: int | None,
    expected_error: str | None,
) -> None:
    """
    只有可解析索引或原始出口的 404 可以结束降级；镜像失败后仍保留原始出口的结果。
    """
    settings = {
        "GITHUB_PROXY": "https://mirror.example",
        "PROXY_HOST": None,
        "PROXY": None,
    }
    monkeypatch.setattr(
        plugin_client_module,
        "get_runtime_setting",
        lambda key, default=None: settings.get(key, default),
    )
    calls: list[str] = []
    index_url = "https://raw.githubusercontent.com/example/repo/main/package.v3.json"
    mirror_url = f"https://mirror.example/{index_url}"

    def response_for(url: str) -> tuple[int, bytes] | None:
        """
        记录出口顺序并按实际请求地址选择离线响应。
        """
        calls.append(url)
        if url == mirror_url:
            return mirror
        assert url == index_url
        if origin_status is None:
            return None
        return origin_status, b'{"DemoPlugin": {"version": "1.0.0"}}'

    @contextmanager
    def sync_stream(*, url: str, raise_exception: bool):
        """
        用离线响应替代同步出站流。
        """
        assert raise_exception is True
        raw = response_for(url)
        yield _SyncStreamResponse(raw[1], status_code=raw[0]) if raw else None

    @asynccontextmanager
    async def async_stream(*, url: str, raise_exception: bool):
        """
        用离线响应替代异步出站流。
        """
        assert raise_exception is True
        raw = response_for(url)
        yield _AsyncStreamResponse(raw[1], status_code=raw[0]) if raw else None

    monkeypatch.setattr(
        plugin_client_module, "RequestUtils", Mock(return_value=SimpleNamespace(get_stream=sync_stream))
    )
    monkeypatch.setattr(
        plugin_client_module, "AsyncRequestUtils", Mock(return_value=SimpleNamespace(get_stream=async_stream))
    )
    transport = PluginMarketTransport()

    async def fetch():
        """
        通过实际传输入口执行同步或异步索引读取。
        """
        if mode == "sync":
            return transport._fetch_plugin_index_sync(index_url, None)
        return await transport._fetch_plugin_index_async(index_url, None)

    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            await fetch()
    elif mirror == (200, b"{}"):
        assert await fetch() == {}
    elif origin_status == 404:
        assert await fetch() is None
    else:
        assert await fetch() == {"DemoPlugin": {"version": "1.0.0"}}

    if mirror == (200, b"{}"):
        assert calls == [mirror_url]
    else:
        assert calls == [mirror_url, index_url]


@pytest.mark.asyncio
async def test_sync_and_async_plugin_indexes_share_request_and_result(
    monkeypatch,
) -> None:
    """
    同步与异步索引读取必须使用同一请求计划并返回相同三态结果。
    """
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/policy-repository"
    response = {"DemoPlugin": {"version": "1.2.3"}}
    sync_requests: list[tuple[str, dict]] = []
    async_requests: list[tuple[str, dict]] = []

    def sync_request(url: str, *, headers: dict):
        """
        记录同步入口收到的 URL 与请求头。
        """
        sync_requests.append((url, headers))
        return response

    async def async_request(url: str, *, headers: dict):
        """
        记录异步入口收到的 URL 与请求头。
        """
        async_requests.append((url, headers))
        return response

    monkeypatch.setattr(
        helper,
        SYNC_INDEX_REQUEST,
        sync_request,
    )
    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        async_request,
    )

    helper.get_plugin_index_result.cache_clear()
    await helper.async_get_plugin_index_result.cache_clear()
    sync_result = helper.get_plugins(repo_url, "v3")
    # 同步与异步装饰器按设计共享缓存区；清除后再验证异步 I/O 入口本身。
    await helper.async_get_plugin_index_result.cache_clear()
    async_result = await helper.async_get_plugins(repo_url, "v3")

    assert sync_result == async_result == {
        "DemoPlugin": {"version": "1.2.3"},
    }
    assert sync_requests == async_requests
    assert sync_requests[0][0] == (
        "https://raw.githubusercontent.com/policy-owner/"
        "policy-repository/main/package.v3.json"
    )


@pytest.mark.asyncio
async def test_market_read_warms_source_inventory_cache(monkeypatch) -> None:
    """
    市场目录成功读取后，来源库存不得再次请求同一仓库代际。
    """
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/shared-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return {"DemoPlugin": {"version": "1.2.3"}}

    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        request,
    )
    await helper.async_get_plugin_index_result.cache_clear()

    market_result = await helper.async_get_plugins(repo_url, "v3")
    inventory_result = await helper.async_get_plugin_index_result(repo_url, "v3")

    assert market_result == inventory_result
    assert requests == 1


def test_sync_force_refresh_bypasses_index_cache(
    monkeypatch,
) -> None:
    """
    同步强刷必须绕过唯一索引缓存。
    """
    helper = PluginMarketTransport()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/sync-force-refresh"
    version = "1.0.0"
    requests = 0

    def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return {"DemoPlugin": {"version": version}}

    monkeypatch.setattr(
        helper,
        SYNC_INDEX_REQUEST,
        request,
    )
    helper.get_plugin_index_result.cache_clear()

    assert client.get_plugins(repo_url, "v3") == {
        "DemoPlugin": {"version": "1.0.0"},
    }
    assert client.get_plugins(repo_url, "v3") == {
        "DemoPlugin": {"version": "1.0.0"},
    }
    assert requests == 1

    version = "2.0.0"
    assert client.get_plugins(repo_url, "v3", force=True) == {
        "DemoPlugin": {"version": "2.0.0"},
    }
    assert requests == 2


@pytest.mark.asyncio
async def test_async_force_refresh_bypasses_index_cache(
    monkeypatch,
) -> None:
    """
    异步强刷必须绕过唯一索引缓存。
    """
    helper = PluginMarketTransport()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/async-force-refresh"
    version = "1.0.0"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return {"DemoPlugin": {"version": version}}

    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        request,
    )
    await helper.async_get_plugin_index_result.cache_clear()

    assert await client.async_get_plugins(repo_url, "v3") == {
        "DemoPlugin": {"version": "1.0.0"},
    }
    assert await client.async_get_plugins(repo_url, "v3") == {
        "DemoPlugin": {"version": "1.0.0"},
    }
    assert requests == 1

    version = "2.0.0"
    assert await client.async_get_plugins(repo_url, "v3", force=True) == {
        "DemoPlugin": {"version": "2.0.0"},
    }
    assert requests == 2


@pytest.mark.asyncio
async def test_absent_plugin_generation_is_cached(monkeypatch) -> None:
    """
    明确不存在的代际是稳定事实，后续来源检查不得重复请求。
    """
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/absent-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return None

    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        request,
    )
    await helper.async_get_plugin_index_result.cache_clear()

    first = await helper.async_get_plugin_index_result(repo_url, "v2")
    second = await helper.async_get_plugin_index_result(repo_url, "v2")

    assert first is second is None
    assert requests == 1


@pytest.mark.asyncio
async def test_index_cache_retains_multi_market_generation_working_set(
    monkeypatch,
) -> None:
    """
    数十个市场的多代索引不能因缓存容量不足立即重复出站。
    """
    helper = PluginMarketTransport()
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return {"DemoPlugin": {"version": "1.0.0"}}

    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        request,
    )
    await helper.async_get_plugin_index_result.cache_clear()
    targets = [
        (f"https://github.com/cache-owner/repository-{index}", generation)
        for index in range(100)
        for generation in ("v3", "v2", None)
    ]

    for repo_url, generation in targets:
        await helper.async_get_plugin_index_result(repo_url, generation)
    await helper.async_get_plugin_index_result(*targets[0])

    assert requests == len(targets)


@pytest.mark.parametrize("content_length", [None, str(PLUGIN_INDEX_MAX_BYTES)])
def test_sync_plugin_index_stream_accepts_exact_byte_limit(content_length) -> None:
    """
    同步索引以解压后实际字节计数，恰好达到上限仍可读取。
    """
    content = b"x" * PLUGIN_INDEX_MAX_BYTES
    response = _SyncStreamResponse(content, content_length)

    status_code, text = PluginMarketTransport._PluginMarketTransport__read_plugin_index_response(
        response
    )

    assert status_code == 200
    assert len(text) == PLUGIN_INDEX_MAX_BYTES


@pytest.mark.parametrize("content_length", [None, "invalid", str(PLUGIN_INDEX_MAX_BYTES)])
def test_sync_plugin_index_stream_rejects_actual_byte_overflow(content_length) -> None:
    """
    缺失、伪造或偏小的声明长度都不能绕过同步实际读取上限。
    """
    response = _SyncStreamResponse(
        b"x" * (PLUGIN_INDEX_MAX_BYTES + 1),
        content_length,
    )

    with pytest.raises(RuntimeError, match="插件索引响应超过"):
        PluginMarketTransport._PluginMarketTransport__read_plugin_index_response(
            response
        )


def test_sync_plugin_index_stream_rejects_declared_byte_overflow() -> None:
    """
    可信声明已超限时无需读取响应体。
    """
    response = _SyncStreamResponse(
        b"",
        str(PLUGIN_INDEX_MAX_BYTES + 1),
    )

    with pytest.raises(RuntimeError, match="插件索引响应超过"):
        PluginMarketTransport._PluginMarketTransport__read_plugin_index_response(
            response
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", [None, str(PLUGIN_INDEX_MAX_BYTES)])
async def test_async_plugin_index_stream_accepts_exact_byte_limit(content_length) -> None:
    """
    异步索引与同步入口共享恰好达到上限时仍成功的合同。
    """
    response = _AsyncStreamResponse(
        b"x" * PLUGIN_INDEX_MAX_BYTES,
        content_length,
    )

    status_code, text = await PluginMarketTransport._PluginMarketTransport__async_read_plugin_index_response(
        response
    )

    assert status_code == 200
    assert len(text) == PLUGIN_INDEX_MAX_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", [None, "invalid", str(PLUGIN_INDEX_MAX_BYTES)])
async def test_async_plugin_index_stream_rejects_actual_byte_overflow(content_length) -> None:
    """
    异步流式读取同样不能被缺失或不可信的声明长度绕过。
    """
    response = _AsyncStreamResponse(
        b"x" * (PLUGIN_INDEX_MAX_BYTES + 1),
        content_length,
    )

    with pytest.raises(RuntimeError, match="插件索引响应超过"):
        await PluginMarketTransport._PluginMarketTransport__async_read_plugin_index_response(
            response
        )


@pytest.mark.parametrize(
    ("entry_count", "accepted"),
    [
        (PLUGIN_INDEX_MAX_ENTRIES, True),
        (PLUGIN_INDEX_MAX_ENTRIES + 1, False),
    ],
)
def test_plugin_index_entry_count_boundary(entry_count: int, accepted: bool) -> None:
    """
    索引条目总数采用含上限边界，超出后整仓读取失败。
    """
    content = json.dumps(
        {
            f"Plugin{index}": {"version": "1.0.0"}
            for index in range(entry_count)
        }
    )

    result = PluginMarketTransport()._resolve_plugin_index_response(200, content)

    assert (result is not None) is accepted
    if result is not None:
        assert len(result) == PLUGIN_INDEX_MAX_ENTRIES


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            '\ufeff{"BomPlugin": {"version": "1.0.0"}}',
            {"BomPlugin": {"version": "1.0.0"}},
        ),
        (
            json.dumps(
                {
                    "plugins": [
                        {
                            "id": "WrappedPlugin",
                            "version": "1.2.3",
                        }
                    ]
                }
            ),
            {"WrappedPlugin": {"id": "WrappedPlugin", "version": "1.2.3"}},
        ),
    ],
)
def test_plugin_index_accepts_bom_and_wrapped_entries(
    content: str,
    expected: dict,
) -> None:
    """
    插件索引应兼容 UTF-8 BOM 与 plugins 数组包装格式。
    """
    assert (
        PluginMarketTransport()._resolve_plugin_index_response(200, content)
        == expected
    )


def test_plugin_index_isolates_bad_entries_and_preserves_unknown_fields() -> None:
    """
    决策字段非法只隔离该条目，展示字段降级且未知字段保持前向兼容。
    """
    history = {str(index): "change" for index in range(513)}
    content = json.dumps(
        {
            "GoodPlugin": {
                "version": "1.0.0",
                "release": True,
                "future_contract": {"enabled": True},
            },
            "BadRelease": {"version": "1.0.0", "release": "false"},
            "NullRelease": {"version": "1.0.0", "release": None},
            "BadLevel": {"version": "1.0.0", "level": "99"},
            "Bad-ID": {"version": "1.0.0"},
            "MismatchedInnerId": {
                "id": "OtherPlugin",
                "version": "1.0.0",
            },
            "InvalidInnerId": {"id": "Bad-ID", "version": "1.0.0"},
            "CanonicalInnerId": {
                "id": "canonicalinnerid",
                "version": "1.0.0",
            },
            "NotObject": [],
            "DisplayFallback": {
                "version": "1.0.0",
                "description": "x" * 4097,
                "labels": ["x"] * 65,
                "history": history,
            },
        }
    )

    result = PluginMarketTransport()._resolve_plugin_index_response(200, content)

    assert set(result or {}) == {
        "GoodPlugin",
        "CanonicalInnerId",
        "DisplayFallback",
    }
    assert result["GoodPlugin"]["future_contract"] == {"enabled": True}
    assert result["CanonicalInnerId"]["id"] == "CanonicalInnerId"
    assert set(result["DisplayFallback"]) == {"version"}


def test_plugin_index_parse_error_does_not_log_response_payload(monkeypatch) -> None:
    """
    解析失败日志不得回显可能包含凭据或超长内容的原始响应。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.logger.warning",
        lambda message: messages.append(message),
    )

    result = PluginMarketTransport()._resolve_plugin_index_response(
        200,
        'not-json-PRIVATE-PAYLOAD',
    )

    assert result is None
    assert messages
    assert all("PRIVATE-PAYLOAD" not in message for message in messages)


def test_plugin_index_parse_error_logs_source_without_cache_buster(monkeypatch) -> None:
    """
    解析失败日志应给出索引来源便于定位仓库，同时去掉缓存穿透参数与响应正文。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.logger.warning",
        lambda message: messages.append(message),
    )

    result = PluginMarketTransport()._resolve_plugin_index_response(
        200,
        "not-json-PRIVATE-PAYLOAD",
        source=(
            "https://raw.githubusercontent.com/example/repo/main/package.v2.json"
            "?_refresh=123"
        ),
    )

    assert result is None
    assert any("package.v2.json" in message for message in messages)
    assert all("_refresh=" not in message for message in messages)
    assert all("PRIVATE-PAYLOAD" not in message for message in messages)


def test_plugin_index_source_log_drops_credentials(monkeypatch) -> None:
    """
    镜像地址可能带有凭据，失败日志只能保留主机与路径。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.logger.warning",
        lambda message: messages.append(message),
    )

    result = PluginMarketTransport()._resolve_plugin_index_response(
        200,
        "not-json",
        source=(
            "https://user:secret@mirror.example.com/example/repo/"
            "package.v2.json?_refresh=1"
        ),
    )

    assert result is None
    assert any(
        "mirror.example.com/example/repo/package.v2.json" in message
        for message in messages
    )
    assert all("secret" not in message for message in messages)


def test_plugin_index_read_failure_logs_requested_url(monkeypatch) -> None:
    """
    索引读取降级链路必须把实际请求地址带进解析失败日志。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.logger.warning",
        lambda message: messages.append(message),
    )

    class FakeResponse:
        """占位响应对象，实际内容由读取函数替换。"""

    class FakeStream:
        """提供 with 语义的占位流。"""

        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_args):
            return False

    class FakeRequestUtils:
        """跳过真实网络请求的请求器。"""

        def __init__(self, **_kwargs):
            pass

        def get_stream(self, **_kwargs):
            return FakeStream()

    monkeypatch.setattr(plugin_client_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(
        PluginMarketTransport,
        "_build_github_request_strategies",
        staticmethod(lambda url, headers=None, timeout=None: [("直连", url, {})]),
    )
    monkeypatch.setattr(
        PluginMarketTransport,
        "_PluginMarketTransport__read_plugin_index_response",
        classmethod(lambda cls, _response: (200, "not-json")),
    )

    package_url = (
        "https://raw.githubusercontent.com/example/repo/main/package.v2.json"
        "?_refresh=1"
    )
    with pytest.raises(plugin_client_module._PluginIndexReadError):
        PluginMarketTransport._PluginMarketTransport__request_plugin_index_with_fallback(
            package_url
        )

    assert any(
        "example/repo/main/package.v2.json" in message for message in messages
    )
    assert all("_refresh=" not in message for message in messages)


def test_local_plugin_index_parse_error_logs_package_file(monkeypatch, tmp_path) -> None:
    """
    本地索引非法时，解析失败日志应指出具体插件包文件。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "app.adapters.external.plugin.client.logger.warning",
        lambda message: messages.append(message),
    )
    package_file = tmp_path / "package.v3.json"
    package_file.write_text("not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="格式无效"):
        PluginMarketTransport._PluginMarketTransport__get_local_package(
            tmp_path,
            "v3",
        )

    assert any(str(package_file) in message for message in messages)


def test_plugin_index_rejects_excessive_json_nesting() -> None:
    """
    深度异常的 JSON 必须形成读取失败，不能把解析器异常泄漏到市场调用方。
    """
    content = '{"DemoPlugin":{"version":"1.0.0","future":' + "[" * 2000
    content += "0" + "]" * 2000 + "}}"

    result = PluginMarketTransport()._resolve_plugin_index_response(200, content)

    assert result is None


def test_local_plugin_index_rejects_oversize_and_invalid_files(tmp_path) -> None:
    """
    已存在的本地索引超限或非法时必须失败，不能形成部分成功库存。
    """
    package_file = tmp_path / "package.v3.json"
    package_file.write_bytes(b"x" * (PLUGIN_INDEX_MAX_BYTES + 1))

    with pytest.raises(RuntimeError, match="插件索引超过"):
        PluginMarketTransport._PluginMarketTransport__get_local_package(
            tmp_path,
            "v3",
        )

    package_file.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="格式无效"):
        PluginMarketTransport._PluginMarketTransport__get_local_package(
            tmp_path,
            "v3",
        )


@pytest.mark.parametrize(
    ("status_code", "content", "expected"),
    [
        (404, "404: Not Found", {}),
        (500, "upstream failed", None),
        (
            200,
            '{"DemoPlugin": {"version": "1.0.0"}}',
            {"DemoPlugin": {"version": "1.0.0"}},
        ),
        (200, "not-json", None),
        (200, "[]", None),
    ],
)
def test_plugin_index_response_preserves_status_contract(
    status_code: int,
    content: str,
    expected: dict | None,
) -> None:
    """
    统一响应解析必须保留 404 空索引、失败和有效字典的既有区别。
    """
    result = PluginMarketTransport()._resolve_plugin_index_response(status_code, content)

    assert result == expected


@pytest.mark.parametrize(
    ("status_code", "content", "expected"),
    [
        (200, '{"DemoPlugin": {"version": "1.2.3"}}', {"DemoPlugin": {"version": "1.2.3"}}),
        (404, "404: Not Found", None),
    ],
)
def test_plugin_index_result_preserves_read_state(
    monkeypatch,
    status_code: int,
    content: str,
    expected: dict | None,
) -> None:
    """
    只读入口以值和 None 区分真实索引与确定不存在。
    """
    helper = PluginMarketTransport()
    repo_url = f"https://github.com/policy-owner/policy-repository-{status_code}"

    def request(_url: str, *, headers: dict):
        return helper._resolve_plugin_index_result((status_code, content))

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    helper.get_plugin_index_result.cache_clear()

    result = helper.get_plugin_index_result(repo_url, "v3")

    assert result == expected


@pytest.mark.parametrize(
    ("status_code", "content", "message"),
    [
        (500, "upstream failed", "插件索引请求失败：HTTP 500"),
        (200, "not-json", "插件索引响应格式无效"),
    ],
)
def test_plugin_index_result_raises_for_unusable_reads(
    monkeypatch,
    status_code: int,
    content: str,
    message: str,
) -> None:
    """
    不可判定读取必须抛错，由应用库存统一记录失败事实。
    """
    helper = PluginMarketTransport()

    def request(_url: str, *, headers: dict):
        return helper._resolve_plugin_index_result((status_code, content))

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    helper.get_plugin_index_result.cache_clear()

    with pytest.raises(RuntimeError, match=message):
        helper.get_plugin_index_result(
            f"https://github.com/policy-owner/policy-failed-{status_code}",
            "v3",
        )


def test_invalid_plugin_index_is_not_cached(monkeypatch) -> None:
    """
    非法索引每次都应重新读取，避免失败载荷占用正常缓存周期。
    """
    helper = PluginMarketTransport()
    requests = 0

    def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return helper._resolve_plugin_index_result((200, "not-json"))

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    helper.get_plugin_index_result.cache_clear()

    for _ in range(2):
        with pytest.raises(RuntimeError, match="插件索引响应格式无效"):
            helper.get_plugin_index_result(
                "https://github.com/policy-owner/invalid-index-cache",
                "v3",
            )

    assert requests == 2


@pytest.mark.asyncio
async def test_async_plugin_index_result_preserves_absent_state(monkeypatch) -> None:
    """
    异步只读入口也必须保留 404 不存在事实。
    """
    helper = PluginMarketTransport()

    async def request(_url: str, *, headers: dict):
        return None

    monkeypatch.setattr(
        helper,
        ASYNC_INDEX_REQUEST,
        request,
    )
    await helper.async_get_plugin_index_result.cache_clear()

    result = await helper.async_get_plugin_index_result(
        "https://github.com/policy-owner/policy-repository-async",
        "v3",
    )

    assert result is None


@pytest.mark.asyncio
async def test_async_plugin_index_failure_is_not_reported_as_background_task_error(
    monkeypatch,
) -> None:
    """
    异步索引失败交给市场调用方处理，不应再被任务登记器重复上报。
    """
    helper = PluginMarketTransport()
    registry = TaskRegistry()
    reports: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    async def request(_url: str, *, headers: dict):
        return helper._resolve_plugin_index_result((200, "not-json"))

    monkeypatch.setattr(helper, ASYNC_INDEX_REQUEST, request)
    monkeypatch.setattr(
        plugin_client_module,
        "get_task_registry",
        lambda: registry,
    )
    await helper.async_get_plugin_index_result.cache_clear()
    loop.set_exception_handler(lambda _, context: reports.append(context))

    try:
        with pytest.raises(RuntimeError, match="插件索引响应格式无效"):
            await helper.async_get_plugin_index_result(
                "https://github.com/policy-owner/async-failed-index",
                "v3",
            )
        await asyncio.sleep(0)
        assert reports == []
        assert registry.records == ()
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_async_plugin_index_requests_are_single_flight(monkeypatch) -> None:
    """
    同一仓库和代际的并发索引读取只能产生一次出站请求。
    """
    helper = PluginMarketTransport()
    repo_url = "https://github.com/single-flight-owner/single-flight-repo"
    started = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        started.set()
        await release.wait()
        return {"DemoPlugin": {"version": "1.2.3"}}

    monkeypatch.setattr(helper, ASYNC_INDEX_REQUEST, request)
    await helper.async_get_plugin_index_result.cache_clear()

    first = asyncio.create_task(
        helper.async_get_plugin_index_result(repo_url, "v3")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(
        helper.async_get_plugin_index_result(
            "https://github.com/single-flight-owner/single-flight-repo/",
            "v3",
        )
    )
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [
        {"DemoPlugin": {"version": "1.2.3"}},
        {"DemoPlugin": {"version": "1.2.3"}},
    ]
    assert requests == 1


@pytest.mark.asyncio
async def test_async_plugin_index_requests_share_process_gate(monkeypatch) -> None:
    """
    不同目录刷新入口也必须受同一个进程级索引请求闸门约束。
    """
    helper = PluginMarketTransport()
    monkeypatch.setattr(
        plugin_client_module,
        "_PLUGIN_INDEX_FETCH_GATE",
        threading.BoundedSemaphore(2),
    )
    active = 0
    peak = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def request(_url: str, *, headers: dict):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        await release.wait()
        active -= 1
        return {"DemoPlugin": {"version": "1.2.3"}}

    monkeypatch.setattr(helper, ASYNC_INDEX_REQUEST, request)
    await helper.async_get_plugin_index_result.cache_clear()
    tasks = [
        asyncio.create_task(
            helper.async_get_plugin_index_result(
                f"https://github.com/process-gate-owner/repository-{index}",
                "v3",
            )
        )
        for index in range(4)
    ]

    await asyncio.wait_for(started.wait(), timeout=1)
    assert active == 2
    release.set()
    results = await asyncio.gather(*tasks)

    assert peak == 2
    assert len(results) == 4


def test_plugin_index_result_propagates_adapter_exception(monkeypatch) -> None:
    """
    请求异常必须传播给应用库存统一转换为失败事实。
    """
    helper = PluginMarketTransport()

    def request(_url: str, *, headers: dict):
        raise OSError("socket closed")

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    helper.get_plugin_index_result.cache_clear()

    with pytest.raises(OSError, match="socket closed"):
        helper.get_plugin_index_result(
            "https://github.com/policy-owner/policy-repository-exception",
            "v3",
        )


def test_plugin_market_client_exposes_index_result_port() -> None:
    """
    市场客户端应原样转发索引读取结果并保留只读边界。
    """
    expected = {"DemoPlugin": {"version": "1.2.3"}}

    class FakeHelper:
        """
        提供固定插件索引结果，隔离市场查询与状态消费。
        """
        def get_plugin_index_result(self, repo_url: str, package_version: str | None):
            """
            返回预设的索引结果对象，检查客户端是否保留端口结果。
            """
            return expected

    client = PluginMarketClient(FakeHelper())

    assert client.get_plugin_index_result("https://github.com/example/repo", "v3") is expected
