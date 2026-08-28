"""站点资源与字幕搜索的统一逐页任务编排测试。"""

import asyncio
from types import SimpleNamespace

import pytest

import app.chain.search.provider as search_module
from app.chain.search import SearchChain


class _Progress:
    """提供不访问 Redis 的异步搜索进度替身。"""

    def __init__(self, *_args, **_kwargs) -> None:
        """接受生产构造参数但不创建外部资源。"""

    async def start(self) -> None:
        """模拟开始进度。"""

    async def update(self, **_kwargs) -> None:
        """模拟更新进度。"""

    async def end(self) -> None:
        """模拟结束进度。"""


def _make_chain() -> SearchChain:
    """构造只包含逐页搜索所需运行时配置的 SearchChain。"""
    chain = object.__new__(SearchChain)
    chain.runtime_config = SimpleNamespace(search_threadpool_size=2)
    return chain


@pytest.mark.asyncio
async def test_site_page_iterator_cancels_and_waits_pending_requests() -> None:
    """调用方提前关闭迭代器时，统一编排器必须收口其他站点请求。"""
    chain = _make_chain()
    blocked_started = asyncio.Event()
    blocked_cancelled = asyncio.Event()
    task_names: list[str] = []

    async def search_page(site: dict, _page: int) -> list[str]:
        """让一个站点立即完成，另一个停留到被编排器取消。"""
        task = asyncio.current_task()
        task_names.append(task.get_name() if task else "")
        if site["id"] == 1:
            return ["ready"]
        blocked_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            blocked_cancelled.set()
            raise

    iterator = chain._iter_site_page_results(
        indexer_sites=[{"id": 1}, {"id": 2}],
        search_pages=[0],
        search_page=search_page,
        should_continue=lambda _site, _results: False,
        task_owner="test.search.site_page",
    )

    assert await anext(iterator) == ({"id": 1}, 0, ["ready"], False)
    await blocked_started.wait()
    await iterator.aclose()

    assert blocked_cancelled.is_set()
    assert task_names == ["test.search.site_page", "test.search.site_page"]


@pytest.mark.asyncio
async def test_subtitle_list_and_stream_share_page_continuation(monkeypatch) -> None:
    """字幕列表与流式入口应复用同一续页规则、页序和停止条件。"""
    chain = _make_chain()
    site = {"id": 7, "name": "SubtitleSite", "subtitles": {"page_size": 2}}
    calls: list[int] = []

    class _Sites:
        """返回固定字幕站点的异步索引替身。"""

        async def async_get_indexers(self) -> list[dict]:
            """返回唯一启用的字幕站点。"""
            return [site]

    async def search_subtitles(*, page: int, **_kwargs) -> list[str]:
        """第一页满页触发续页，第二页不足一页后停止。"""
        calls.append(page)
        return ["one", "two"] if page == 0 else ["three"]

    monkeypatch.setattr(search_module, "SitesHelper", _Sites)
    monkeypatch.setattr(search_module, "AsyncProgressHelper", _Progress)
    monkeypatch.setattr(
        search_module,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: [7]),
    )
    chain._build_search_pages = lambda _page: [0, 1, 2]
    chain._should_continue_subtitle_search_pages = lambda *, site, page_results: len(page_results) == 2
    chain.async_search_subtitles = search_subtitles

    listed = await chain._async_search_subtitles_all_sites(keyword="demo")
    assert listed == ["one", "two", "three"]
    assert calls == [0, 1]

    calls.clear()
    events = [event async for event in chain._async_search_subtitles_all_sites_stream(keyword="demo")]
    appended = [event for event in events if event["type"] == "append"]

    assert [event["page"] for event in appended] == [0, 1]
    assert [event["items"] for event in appended] == [
        ["one", "two"],
        ["three"],
    ]
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_torrent_list_and_stream_share_provider_facts(monkeypatch) -> None:
    """列表与流式入口必须消费同一批 provider 事实并保持插件事件顺序。"""
    chain = _make_chain()
    site = {"id": 8, "name": "TorrentSite"}
    plugin_calls: list[dict] = []
    site_calls: list[dict] = []

    class _Sites:
        """返回固定资源站点的异步索引替身。"""

        async def async_get_indexers(self) -> list[dict]:
            """返回唯一启用的资源站点。"""
            return [site]

    async def search_plugins(**kwargs) -> list[str]:
        """记录插件搜索次数并返回稳定资源。"""
        plugin_calls.append(kwargs)
        return ["plugin"]

    async def search_site(**kwargs) -> list[str]:
        """记录站点搜索次数并返回稳定资源。"""
        site_calls.append(kwargs)
        return ["site"]

    monkeypatch.setattr(search_module, "SitesHelper", _Sites)
    monkeypatch.setattr(search_module, "AsyncProgressHelper", _Progress)
    monkeypatch.setattr(
        search_module,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: [8]),
    )
    chain._build_search_pages = lambda _page: [2]
    chain._should_continue_search_pages = lambda **_kwargs: False
    chain.async_search_plugin_torrents = search_plugins
    chain.async_search_site_torrents = search_site

    listed = await chain._SearchChain__async_search_all_sites(
        keyword="demo",
        page=2,
    )
    events = [
        event
        async for event in chain._SearchChain__async_search_all_sites_stream(
            keyword="demo",
            page=2,
        )
    ]

    assert listed == ["plugin", "site"]
    assert [event["type"] for event in events] == [
        "append",
        "progress",
        "append",
    ]
    assert events[0]["items"] == ["plugin"]
    assert events[0]["page"] == 2
    assert [item for event in events if event["type"] == "append" for item in event["items"]] == listed
    assert len(plugin_calls) == 2
    assert len(site_calls) == 2


@pytest.mark.asyncio
async def test_provider_stream_close_always_ends_progress(monkeypatch) -> None:
    """调用方在初始进度后关闭流时也必须终结异步进度。"""
    chain = _make_chain()
    ended: list[bool] = []

    class _TrackedProgress(_Progress):
        """记录进度终结次数的替身。"""

        async def end(self) -> None:
            """记录进度已进入唯一终态。"""
            ended.append(True)

    monkeypatch.setattr(search_module, "AsyncProgressHelper", _TrackedProgress)

    async def search_page(_site: dict, _page: int) -> list[str]:
        """关闭发生在首个站点请求前，本函数不应执行。"""
        raise AssertionError("站点请求不应在初始进度消费前执行")

    iterator = chain._iter_provider_events(
        keyword="demo",
        indexer_sites=[{"id": 1, "name": "Site"}],
        search_pages=[0],
        initial_items=[],
        initial_page=None,
        search_page=search_page,
        should_continue=lambda _site, _items: False,
        task_owner="test.search.provider.close",
        subtitle=False,
    )

    assert (await anext(iterator))["type"] == "progress"
    await iterator.aclose()

    assert ended == [True]


@pytest.mark.asyncio
async def test_provider_failure_always_ends_progress(monkeypatch) -> None:
    """站点异常向调用方传播时必须先收口任务和异步进度。"""
    chain = _make_chain()
    ended: list[bool] = []

    class _TrackedProgress(_Progress):
        """记录异常路径进度终结次数的替身。"""

        async def end(self) -> None:
            """记录异常路径已进入唯一终态。"""
            ended.append(True)

    monkeypatch.setattr(search_module, "AsyncProgressHelper", _TrackedProgress)

    async def search_page(_site: dict, _page: int) -> list[str]:
        """模拟 provider 边界异常。"""
        raise RuntimeError("provider failed")

    iterator = chain._iter_provider_events(
        keyword="demo",
        indexer_sites=[{"id": 1, "name": "Site"}],
        search_pages=[0],
        initial_items=[],
        initial_page=None,
        search_page=search_page,
        should_continue=lambda _site, _items: False,
        task_owner="test.search.provider.failure",
        subtitle=False,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        async for _event in iterator:
            pass

    assert ended == [True]
