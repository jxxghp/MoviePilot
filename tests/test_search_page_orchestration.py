"""站点资源与字幕搜索的统一逐页任务编排测试。"""

import asyncio
from types import SimpleNamespace

import pytest

import app.chain.search as search_module
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
    chain._should_continue_subtitle_search_pages = (
        lambda *, site, page_results: len(page_results) == 2
    )
    chain.async_search_subtitles = search_subtitles

    listed = await chain._SearchChain__async_search_subtitles_all_sites(
        keyword="demo"
    )
    assert listed == ["one", "two", "three"]
    assert calls == [0, 1]

    calls.clear()
    events = [
        event
        async for event in chain._SearchChain__async_search_subtitles_all_sites_stream(
            keyword="demo"
        )
    ]
    appended = [event for event in events if event["type"] == "append"]

    assert [event["page"] for event in appended] == [0, 1]
    assert [event["items"] for event in appended] == [
        ["one", "two"],
        ["three"],
    ]
    assert calls == [0, 1]
