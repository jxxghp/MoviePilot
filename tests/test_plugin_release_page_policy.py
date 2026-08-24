"""插件 Release 同步与异步分页策略一致性测试。"""

import pytest

from app.adapters.external.market import PluginHelper


class _PageResponse:
    """提供同步与异步分页入口共同消费的最小响应合同。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        """保存待返回的 JSON 数据和 HTTP 状态。"""
        self._payload = payload
        self.status_code = status_code

    def json(self):
        """返回当前页预置的 JSON 数据。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _release_payload(size: int, *, offset: int = 0) -> list[dict]:
    """生成可区分页次的 GitHub Release 最小响应。"""
    return [
        {
            "tag_name": f"DemoPlugin_v1.0.{offset + index}",
            "assets": [
                {"name": f"demoplugin_v1.0.{offset + index}.zip"},
            ],
        }
        for index in range(size)
    ]


@pytest.mark.asyncio
async def test_sync_and_async_release_pages_share_policy(monkeypatch) -> None:
    """同步与异步分页必须请求相同页并产生相同规范化仓库快照。"""
    helper = PluginHelper()
    repo_url = "https://github.com/policy-owner/release-repository"
    responses = [
        _PageResponse(_release_payload(100)),
        _PageResponse(_release_payload(1, offset=100)),
    ]
    sync_requests: list[tuple[str, dict]] = []
    async_requests: list[tuple[str, dict]] = []

    def sync_request(url: str, **kwargs):
        """记录同步分页请求并按页返回响应。"""
        sync_requests.append((url, kwargs))
        return responses[len(sync_requests) - 1]

    async def async_request(url: str, **kwargs):
        """记录异步分页请求并采用相同响应序列。"""
        async_requests.append((url, kwargs))
        return responses[len(async_requests) - 1]

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

    helper._get_plugin_repo_releases.cache_clear()
    await helper._async_get_plugin_repo_releases.cache_clear()
    sync_result = helper._get_plugin_repo_releases(repo_url)
    # 两个装饰器共享仓库级缓存；清除后再覆盖异步网络入口。
    await helper._async_get_plugin_repo_releases.cache_clear()
    async_result = await helper._async_get_plugin_repo_releases(repo_url)

    assert sync_result == async_result
    assert len(sync_result or []) == 101
    assert sync_requests == async_requests
    assert [request[0].rsplit("page=", 1)[1] for request in sync_requests] == [
        "1",
        "2",
    ]


@pytest.mark.parametrize(
    ("response", "expected", "release_count"),
    [
        (None, None, 0),
        (_PageResponse([], 200), False, 0),
        (_PageResponse([{"tag_name": "DemoPlugin_v1.0.0"}], 200), False, 1),
        (_PageResponse(_release_payload(100), 200), True, 100),
        (_PageResponse({"message": "bad payload"}, 200), None, 0),
        (_PageResponse([], 503), None, 0),
        (_PageResponse(ValueError("invalid json"), 200), None, 0),
    ],
)
def test_release_page_merge_preserves_stop_and_failure_contract(
    response,
    expected: bool | None,
    release_count: int,
) -> None:
    """统一分页解析必须区分继续、自然结束和整次仓库读取失败。"""
    releases: list[dict] = []

    result = PluginHelper._merge_plugin_release_page(
        "https://github.com/policy-owner/release-repository",
        response,
        releases,
    )

    assert result is expected
    assert len(releases) == release_count
