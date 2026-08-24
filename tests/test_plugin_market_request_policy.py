"""插件市场同步与异步 GitHub 请求降级策略测试。"""

from types import SimpleNamespace

import pytest

from app.adapters.external import market
from app.adapters.external.market import PluginHelper


@pytest.mark.asyncio
async def test_sync_and_async_github_requests_share_fallback_policy(
    monkeypatch,
) -> None:
    """同步与异步请求必须使用相同镜像、代理、直连顺序和参数。"""
    proxy = {"all": "http://proxy.example:7890"}
    monkeypatch.setattr(
        market,
        "settings",
        SimpleNamespace(
            GITHUB_PROXY="https://mirror.example",
            PROXY_HOST="http://proxy.example:7890",
            PROXY=proxy,
        ),
    )
    sync_requests: list[tuple[dict, str]] = []
    async_requests: list[tuple[dict, str]] = []
    response = object()

    class SyncRequest:
        """记录同步请求，并让前两种策略失败以遍历完整顺序。"""

        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        def get_res(self, *, url: str, raise_exception: bool):
            """记录请求目标，第三次返回固定响应。"""
            assert raise_exception is True
            sync_requests.append((self._kwargs, url))
            if len(sync_requests) < 3:
                raise RuntimeError("next strategy")
            return response

    class AsyncRequest:
        """记录异步请求，并采用与同步客户端相同的结果序列。"""

        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        async def get_res(self, *, url: str, raise_exception: bool):
            """记录请求目标，第三次返回固定响应。"""
            assert raise_exception is True
            async_requests.append((self._kwargs, url))
            if len(async_requests) < 3:
                raise RuntimeError("next strategy")
            return response

    monkeypatch.setattr(market, "RequestUtils", SyncRequest)
    monkeypatch.setattr(market, "AsyncRequestUtils", AsyncRequest)

    sync_response = PluginHelper._PluginHelper__request_with_fallback(
        "https://api.example/resource",
        headers={"X-Test": "1"},
        timeout=12,
    )
    async_response = await PluginHelper._PluginHelper__async_request_with_fallback(
        "https://api.example/resource",
        headers={"X-Test": "1"},
        timeout=12,
    )

    assert sync_response is async_response is response
    assert sync_requests == async_requests == [
        (
            {"headers": {"X-Test": "1"}, "timeout": 12},
            "https://mirror.example/https://api.example/resource",
        ),
        (
            {"headers": {"X-Test": "1"}, "proxies": proxy, "timeout": 12},
            "https://api.example/resource",
        ),
        (
            {"headers": {"X-Test": "1"}, "timeout": 12},
            "https://api.example/resource",
        ),
    ]


def test_github_api_request_policy_skips_content_mirror(monkeypatch) -> None:
    """GitHub API 请求必须跳过只用于 raw 内容的镜像站。"""
    monkeypatch.setattr(
        market,
        "settings",
        SimpleNamespace(
            GITHUB_PROXY="https://mirror.example",
            PROXY_HOST=None,
            PROXY=None,
        ),
    )

    strategies = PluginHelper._build_github_request_strategies(
        url="https://api.github.com/repos/example/plugins/releases",
        is_api=True,
    )

    assert strategies == [
        (
            "直连",
            "https://api.github.com/repos/example/plugins/releases",
            {"headers": None, "timeout": 60},
        )
    ]
