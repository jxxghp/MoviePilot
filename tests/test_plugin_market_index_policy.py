"""插件市场索引同步与异步策略一致性测试。"""

from types import SimpleNamespace

import pytest

from app.adapters.external.market import PluginHelper
from app.adapters.external.plugin.client import PluginMarketClient


@pytest.mark.asyncio
async def test_sync_and_async_plugin_indexes_share_request_and_result(
    monkeypatch,
) -> None:
    """同步与异步索引读取必须使用同一请求计划并返回相同三态结果。"""
    helper = PluginHelper()
    repo_url = "https://github.com/policy-owner/policy-repository"
    response = SimpleNamespace(
        status_code=200,
        text='{"DemoPlugin": {"version": "1.2.3"}}',
    )
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
        "_PluginHelper__request_with_fallback",
        sync_request,
    )
    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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
    helper = PluginHelper()
    repo_url = "https://github.com/policy-owner/shared-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return SimpleNamespace(
            status_code=200,
            text='{"DemoPlugin": {"version": "1.2.3"}}',
        )

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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
    helper = PluginHelper()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/sync-force-refresh"
    version = "1.0.0"
    requests = 0

    def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return SimpleNamespace(
            status_code=200,
            text=f'{{"DemoPlugin": {{"version": "{version}"}}}}',
        )

    monkeypatch.setattr(
        helper,
        "_PluginHelper__request_with_fallback",
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
    helper = PluginHelper()
    client = PluginMarketClient(helper)
    repo_url = "https://github.com/policy-owner/async-force-refresh"
    version = "1.0.0"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return SimpleNamespace(
            status_code=200,
            text=f'{{"DemoPlugin": {{"version": "{version}"}}}}',
        )

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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
    helper = PluginHelper()
    repo_url = "https://github.com/policy-owner/absent-index-cache"
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return SimpleNamespace(status_code=404, text="404: Not Found")

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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
    helper = PluginHelper()
    requests = 0

    async def request(_url: str, *, headers: dict):
        nonlocal requests
        requests += 1
        return SimpleNamespace(status_code=200, text='{"DemoPlugin": {}}')

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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


@pytest.mark.parametrize(
    ("status_code", "content", "expected"),
    [
        (404, "404: Not Found", {}),
        (500, "upstream failed", None),
        (200, '{"DemoPlugin": {}}', {"DemoPlugin": {}}),
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
    result = PluginHelper._resolve_plugin_index_response(status_code, content)

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
    helper = PluginHelper()
    repo_url = f"https://github.com/policy-owner/policy-repository-{status_code}"

    def request(_url: str, *, headers: dict):
        return SimpleNamespace(status_code=status_code, text=content)

    monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", request)
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
    helper = PluginHelper()

    def request(_url: str, *, headers: dict):
        return SimpleNamespace(status_code=status_code, text=content)

    monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", request)
    helper.get_plugin_index_result.cache_clear()

    with pytest.raises(RuntimeError, match=message):
        helper.get_plugin_index_result(
            f"https://github.com/policy-owner/policy-failed-{status_code}",
            "v3",
        )


@pytest.mark.asyncio
async def test_async_plugin_index_result_preserves_absent_state(monkeypatch) -> None:
    """异步只读入口也必须保留 404 不存在事实。"""
    helper = PluginHelper()

    async def request(_url: str, *, headers: dict):
        return SimpleNamespace(status_code=404, text="404: Not Found")

    monkeypatch.setattr(
        helper,
        "_PluginHelper__async_request_with_fallback",
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
    helper = PluginHelper()

    def request(_url: str, *, headers: dict):
        raise OSError("socket closed")

    monkeypatch.setattr(helper, "_PluginHelper__request_with_fallback", request)
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
