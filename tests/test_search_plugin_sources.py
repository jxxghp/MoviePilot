import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters.network.http import RequestUtils
from app.chain.search import SearchChain
from app.modules.indexer import IndexerModule
from app.runtime.config import settings
from app.runtime.correlation import CORRELATION_ID_HEADER, correlation_scope


def make_chain() -> SearchChain:
    """构造不触发完整启动流程的搜索链。"""
    chain = object.__new__(SearchChain)
    chain.get_search_page_size = IndexerModule.get_search_page_size
    return chain


def test_search_returns_plugin_results_without_indexer_sites():
    """未配置 PT 站点时，插件资源仍应进入原生资源搜索。"""
    chain = make_chain()
    plugin_item = SimpleNamespace(title="Plugin Result", description="")
    calls = []
    chain.search_plugin_torrents = lambda **kwargs: calls.append(kwargs) or [plugin_item]

    with (
        patch("app.chain.search.provider.get_configured_system_config") as system_config_oper,
        patch("app.chain.search.provider.SitesHelper") as sites_helper,
    ):
        system_config_oper.return_value.get.return_value = []
        sites_helper.return_value.get_indexers.return_value = []
        results = chain._SearchChain__search_all_sites(keyword="keyword")

    assert results == [plugin_item]
    assert len(calls) == 1
    assert calls[0]["keyword"] == "keyword"


def test_search_invokes_plugin_once_with_multiple_indexers():
    """多个 PT 站点不应导致插件资源源被重复搜索。"""
    chain = make_chain()
    plugin_calls = []
    site_calls = []
    chain.search_plugin_torrents = lambda **kwargs: plugin_calls.append(kwargs) or [
        SimpleNamespace(title="Plugin Result", description="")
    ]
    chain.search_site_torrents = lambda **kwargs: site_calls.append(kwargs) or [
        SimpleNamespace(title=f"Site {kwargs['site']['id']}", description="")
    ]

    with (
        patch.object(settings, "SEARCH_RESOURCE_PAGES", 1, create=True),
        patch("app.chain.search.provider.get_configured_system_config") as system_config_oper,
        patch("app.chain.search.provider.SitesHelper") as sites_helper,
        patch("app.chain.search.provider.ProgressHelper") as progress_helper,
    ):
        system_config_oper.return_value.get.return_value = [1, 2]
        sites_helper.return_value.get_indexers.return_value = [
            {"id": 1, "name": "站点一"},
            {"id": 2, "name": "站点二"},
        ]
        progress_helper.return_value = SimpleNamespace(
            start=lambda: None, update=lambda **_kwargs: None, end=lambda: None
        )
        results = chain._SearchChain__search_all_sites(keyword="keyword")

    assert len(plugin_calls) == 1
    assert sorted(call["site"]["id"] for call in site_calls) == [1, 2]
    assert len(results) == 3


def test_sync_site_search_propagates_request_context():
    """同步站点 worker 的出站请求头应保留触发搜索的关联 ID。"""
    chain = make_chain()
    observed_headers = []
    chain.search_plugin_torrents = lambda **_kwargs: []

    def search_site_torrents(**_kwargs):
        RequestUtils().get_res("https://indexer.example/search")
        return []

    def request(_method, _url, **kwargs):
        observed_headers.append(kwargs["headers"])
        return object()

    chain.search_site_torrents = search_site_torrents

    with (
        patch.object(settings, "SEARCH_RESOURCE_PAGES", 1, create=True),
        patch("app.adapters.network.http.requests.request", side_effect=request),
        patch("app.chain.search.provider.get_configured_system_config") as system_config_oper,
        patch("app.chain.search.provider.SitesHelper") as sites_helper,
        patch("app.chain.search.provider.ProgressHelper") as progress_helper,
    ):
        system_config_oper.return_value.get.return_value = [1, 2]
        sites_helper.return_value.get_indexers.return_value = [
            {"id": 1, "name": "站点一"},
            {"id": 2, "name": "站点二"},
        ]
        progress_helper.return_value = SimpleNamespace(
            start=lambda: None, update=lambda **_kwargs: None, end=lambda: None
        )
        with correlation_scope("search-request"):
            chain._SearchChain__search_all_sites(keyword="keyword")

    assert [headers[CORRELATION_ID_HEADER] for headers in observed_headers] == [
        "search-request",
        "search-request",
    ]


def test_async_search_returns_plugin_results_without_indexers():
    """异步搜索应支持只有插件资源源的部署方式。"""
    chain = make_chain()
    plugin_item = SimpleNamespace(title="Plugin Result", description="")
    calls = []

    async def plugin_search(**kwargs):
        calls.append(kwargs)
        return [plugin_item]

    chain.async_search_plugin_torrents = plugin_search
    async def run_search():
        with (
            patch("app.chain.search.provider.get_configured_system_config") as system_config_oper,
            patch("app.chain.search.provider.SitesHelper") as sites_helper,
        ):
            system_config_oper.return_value.get.return_value = []
            sites_helper.return_value.async_get_indexers = AsyncMock(return_value=[])
            return await chain._SearchChain__async_search_all_sites(keyword="keyword")

    results = asyncio.run(run_search())

    assert results == [plugin_item]
    assert len(calls) == 1


def test_async_search_stream_emits_plugin_results_once_without_indexers():
    """流式搜索完成事件不应重复发送插件资源。"""
    chain = make_chain()
    plugin_item = SimpleNamespace(title="Plugin Result", description="")

    async def plugin_search(**_kwargs):
        return [plugin_item]

    chain.async_search_plugin_torrents = plugin_search
    async def collect_events():
        with (
            patch("app.chain.search.provider.get_configured_system_config") as system_config_oper,
            patch("app.chain.search.provider.SitesHelper") as sites_helper,
        ):
            system_config_oper.return_value.get.return_value = []
            sites_helper.return_value.async_get_indexers = AsyncMock(return_value=[])
            return [
                event
                async for event in chain._SearchChain__async_search_all_sites_stream(
                    keyword="keyword"
                )
            ]

    events = asyncio.run(collect_events())

    assert [event["type"] for event in events] == ["append", "done"]
    assert events[0]["items"] == [plugin_item]
    assert events[1]["items"] == []
    assert events[1]["total_items"] == 1
