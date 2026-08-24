"""插件市场索引同步与异步策略一致性测试。"""

from types import SimpleNamespace

import pytest

from app.adapters.external.market import PluginHelper


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

    helper.get_plugins.cache_clear()
    await helper.async_get_plugins.cache_clear()
    sync_result = helper.get_plugins(repo_url, "v3")
    # 同步与异步装饰器按设计共享缓存区；清除后再验证异步 I/O 入口本身。
    await helper.async_get_plugins.cache_clear()
    async_result = await helper.async_get_plugins(repo_url, "v3")

    assert sync_result == async_result == {
        "DemoPlugin": {"version": "1.2.3"},
    }
    assert sync_requests == async_requests
    assert sync_requests[0][0] == (
        "https://raw.githubusercontent.com/policy-owner/"
        "policy-repository/main/package.v3.json"
    )


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
