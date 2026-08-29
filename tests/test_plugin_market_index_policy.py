"""插件市场索引同步与异步策略一致性测试。"""

import json
from contextlib import asynccontextmanager, contextmanager

import pytest

from app.adapters.external.plugin.client import (
    PLUGIN_INDEX_MAX_BYTES,
    PLUGIN_INDEX_MAX_ENTRIES,
    PluginMarketClient,
    PluginMarketTransport,
)

SYNC_INDEX_REQUEST = "_PluginMarketTransport__request_plugin_index_with_fallback"
ASYNC_INDEX_REQUEST = "_PluginMarketTransport__async_request_plugin_index_with_fallback"


class _SyncStreamResponse:
    """提供 requests 流式响应所需的最小测试契约。"""

    def __init__(self, content: bytes, content_length: str | None = None) -> None:
        self.status_code = 200
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self._content = content

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]


class _AsyncStreamResponse:
    """提供 httpx 流式响应所需的最小测试契约。"""

    def __init__(self, content: bytes, content_length: str | None = None) -> None:
        self.status_code = 200
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self._content = content

    async def aiter_bytes(self, chunk_size: int):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]


def test_plugin_index_request_uses_sync_stream_transport(monkeypatch) -> None:
    """同步索引专用请求必须进入流式传输，不能预先加载完整响应体。"""
    calls: list[tuple[dict, str, bool]] = []

    class FakeRequestUtils:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        @contextmanager
        def get_stream(self, url: str, raise_exception: bool):
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

    assert result == (200, '{"DemoPlugin":{"version":"1.0.0"}}')
    assert calls[0][0]["headers"] == {"Accept": "application/json"}
    assert calls[0][2] is True


@pytest.mark.asyncio
async def test_plugin_index_request_uses_async_stream_transport(monkeypatch) -> None:
    """异步索引专用请求与同步入口使用相同的流式资源边界。"""
    calls: list[tuple[dict, str, bool]] = []

    class FakeAsyncRequestUtils:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        @asynccontextmanager
        async def get_stream(self, url: str, raise_exception: bool):
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

    assert result == (200, '{"DemoPlugin":{"version":"1.0.0"}}')
    assert calls[0][0]["headers"] == {"Accept": "application/json"}
    assert calls[0][2] is True


@pytest.mark.asyncio
async def test_sync_and_async_plugin_indexes_share_request_and_result(
    monkeypatch,
) -> None:
    """同步与异步索引读取必须使用同一请求计划并返回相同三态结果。"""
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/policy-repository"
    response = (200, '{"DemoPlugin": {"version": "1.2.3"}}')
    sync_requests: list[tuple[str, dict]] = []
    async_requests: list[tuple[str, dict]] = []

    def sync_request(url: str, *, headers: dict):
        """记录同步入口收到的 URL 与请求头。"""
        sync_requests.append((url, headers))
        return response

    async def async_request(url: str, *, headers: dict):
        """记录异步入口收到的 URL 与请求头。"""
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
    """市场目录成功读取后，来源库存不得再次请求同一仓库代际。"""
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/shared-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 200, '{"DemoPlugin": {"version": "1.2.3"}}'

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
    """同步强刷必须绕过唯一索引缓存。"""
    helper = PluginMarketTransport()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/sync-force-refresh"
    version = "1.0.0"
    requests = 0

    def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 200, f'{{"DemoPlugin": {{"version": "{version}"}}}}'

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
    """异步强刷必须绕过唯一索引缓存。"""
    helper = PluginMarketTransport()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/async-force-refresh"
    version = "1.0.0"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 200, f'{{"DemoPlugin": {{"version": "{version}"}}}}'

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
    """明确不存在的代际是稳定事实，后续来源检查不得重复请求。"""
    helper = PluginMarketTransport()
    repo_url = "https://github.com/policy-owner/absent-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 404, ""

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
    """数十个市场的多代索引不能因缓存容量不足立即重复出站。"""
    helper = PluginMarketTransport()
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 200, '{"DemoPlugin": {"version": "1.0.0"}}'

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
    """同步索引以解压后实际字节计数，恰好达到上限仍可读取。"""
    content = b"x" * PLUGIN_INDEX_MAX_BYTES
    response = _SyncStreamResponse(content, content_length)

    status_code, text = PluginMarketTransport._PluginMarketTransport__read_plugin_index_response(
        response
    )

    assert status_code == 200
    assert len(text) == PLUGIN_INDEX_MAX_BYTES


@pytest.mark.parametrize("content_length", [None, "invalid", str(PLUGIN_INDEX_MAX_BYTES)])
def test_sync_plugin_index_stream_rejects_actual_byte_overflow(content_length) -> None:
    """缺失、伪造或偏小的声明长度都不能绕过同步实际读取上限。"""
    response = _SyncStreamResponse(
        b"x" * (PLUGIN_INDEX_MAX_BYTES + 1),
        content_length,
    )

    with pytest.raises(RuntimeError, match="插件索引响应超过"):
        PluginMarketTransport._PluginMarketTransport__read_plugin_index_response(
            response
        )


def test_sync_plugin_index_stream_rejects_declared_byte_overflow() -> None:
    """可信声明已超限时无需读取响应体。"""
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
    """异步索引与同步入口共享恰好达到上限时仍成功的合同。"""
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
    """异步流式读取同样不能被缺失或不可信的声明长度绕过。"""
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
    """索引条目总数采用含上限边界，超出后整仓读取失败。"""
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


def test_plugin_index_isolates_bad_entries_and_preserves_unknown_fields() -> None:
    """决策字段非法只隔离该条目，展示字段降级且未知字段保持前向兼容。"""
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
    """解析失败日志不得回显可能包含凭据或超长内容的原始响应。"""
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


def test_plugin_index_rejects_excessive_json_nesting() -> None:
    """深度异常的 JSON 必须形成读取失败，不能把解析器异常泄漏到市场调用方。"""
    content = '{"DemoPlugin":{"version":"1.0.0","future":' + "[" * 2000
    content += "0" + "]" * 2000 + "}}"

    result = PluginMarketTransport()._resolve_plugin_index_response(200, content)

    assert result is None


def test_local_plugin_index_rejects_oversize_and_invalid_files(tmp_path) -> None:
    """已存在的本地索引超限或非法时必须失败，不能形成部分成功库存。"""
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
    """统一响应解析必须保留 404 空索引、失败和有效字典的既有区别。"""
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
    """只读入口以值和 None 区分真实索引与确定不存在。"""
    helper = PluginMarketTransport()
    repo_url = f"https://github.com/policy-owner/policy-repository-{status_code}"

    def request(_url: str, *, headers: dict):
        return status_code, content

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
    """不可判定读取必须抛错，由应用库存统一记录失败事实。"""
    helper = PluginMarketTransport()

    def request(_url: str, *, headers: dict):
        return status_code, content

    monkeypatch.setattr(helper, SYNC_INDEX_REQUEST, request)
    helper.get_plugin_index_result.cache_clear()

    with pytest.raises(RuntimeError, match=message):
        helper.get_plugin_index_result(
            f"https://github.com/policy-owner/policy-failed-{status_code}",
            "v3",
        )


def test_invalid_plugin_index_is_not_cached(monkeypatch) -> None:
    """非法索引每次都应重新读取，避免失败载荷占用正常缓存周期。"""
    helper = PluginMarketTransport()
    requests = 0

    def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return 200, "not-json"

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
    """异步只读入口也必须保留 404 不存在事实。"""
    helper = PluginMarketTransport()

    async def request(_url: str, *, headers: dict):
        return 404, ""

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


def test_plugin_index_result_propagates_adapter_exception(monkeypatch) -> None:
    """请求异常必须传播给应用库存统一转换为失败事实。"""
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
    """市场客户端应原样转发索引读取结果并保留只读边界。"""
    expected = {"DemoPlugin": {"version": "1.2.3"}}

    class FakeHelper:
        def get_plugin_index_result(self, repo_url: str, package_version: str | None):
            return expected

    client = PluginMarketClient(FakeHelper())

    assert client.get_plugin_index_result("https://github.com/example/repo", "v3") is expected
