"""Web 手动分页：后端缓存页摘要并恢复过期比较依据，页号与重试由客户端持有。"""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.chain.search import manual as manual_module
from app.chain.search.facade import SearchChain
from app.chain.search.manual import SearchManualOwner, decode_source, encode_source
from app.domain.context import MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def page_clock(monkeypatch):
    """使用可控时钟隔离页摘要缓存，验证失效后的上一页补取。"""
    from app.runtime import cache as cache_module

    clock = [0.0]
    original = cache_module.MemoryTLRUCache.__init__
    def initialize(cache, *args, **kwargs):
        """让测试缓存的过期判断统一使用可控时钟。"""
        original(cache, *args, **kwargs, timer=lambda: clock[0])
    monkeypatch.setattr(cache_module.MemoryTLRUCache, "__init__", initialize)
    monkeypatch.setattr(cache_module.MemoryBackend, "_region_caches", {})
    monkeypatch.setattr(manual_module, "Cache", cache_module.MemoryBackend)
    return clock


@pytest.fixture
def manual_owner(monkeypatch, page_clock):
    """构造离线搜索来源和保存边界，记录页请求并模拟失败。"""
    owner = SearchChain()
    sites = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    monkeypatch.setattr(owner, "_sync_indexers", lambda ids: [site for site in sites if not ids or site["id"] in ids])
    monkeypatch.setattr(owner, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: 100)
    saved = []
    monkeypatch.setattr(owner, "save_last_search_params", lambda **params: saved.append(params))
    async def save_results(contexts):
        """记录首次搜索的保存行为，避免写入真实缓存。"""
        saved.append(len(contexts))
    monkeypatch.setattr(owner, "_async_save_results", save_results)
    calls = []
    failures = set()
    def request(**params):
        """按来源和页号构造结果，同时报告真实搜索观察字段。"""
        site, page = params["site"]["id"], params["page"]
        calls.append((site, params["keyword"], page))
        failed = (site, page) in failures
        report_site_search_outcome(attempted=True, outcome="error" if failed else "success", error="timeout" if failed else None)
        report_site_search_page(raw_count=1, has_more=page < 2)
        return [] if failed else [TorrentInfo(site=site, description=f"{site}-{page}", title=f"Show S01E{page+1:02d}",
                                              enclosure=f"https://site.example/download?id={page}")]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    return owner, calls, (failures, saved)


async def events(iterator):
    """消费完整事件流，供最终页结果和来源状态断言使用。"""
    return [item async for item in iterator]


def site_facts(result):
    """从最终状态中提取支持独立翻页的站点来源。"""
    return [item for item in result[-1]["sources"] if decode_source(item["source"])[0] != "plugin"]


@pytest.fixture
def blocked_site(manual_owner, monkeypatch):
    """阻塞首个站点，其他站点即时完成，用同步信号验证并发和取消收口。"""
    from app.runtime import tasks as tasks_module

    owner, _, _ = manual_owner
    monkeypatch.setattr(owner, "_runtime_config", replace(owner.runtime_config, search_threadpool_size=2))
    monkeypatch.setattr(owner, "_runtime_config_provider", None)
    registry = tasks_module.TaskRegistry()
    monkeypatch.setattr(tasks_module, "_runtime_registry", registry)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = owner.search_site_torrents

    def request(**params):
        """让排序靠前的站点等待释放，证明快站点不受它阻塞。"""
        if params["site"]["id"] != 1:
            return original(**params)
        started.set()
        try:
            assert release.wait(5), "测试未释放慢站点"
            return original(**params)
        finally:
            finished.set()

    monkeypatch.setattr(owner, "search_site_torrents", request)
    yield started, release, finished, registry
    release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_search", [False, True])
@pytest.mark.parametrize("transport", [False, True])
async def test_fast_site_preview_arrives_before_slow_site_and_final_page(
    manual_owner, blocked_site, monkeypatch, media_search, transport,
):
    """标题和精确搜索均先展示快站点，最终过滤前不提交来源页号。"""
    from app.chain.media import MediaChain

    owner, calls, (_, saved) = manual_owner
    started, release, finished, registry = blocked_site
    params = {"keyword": "Show"}
    if media_search:
        media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.TV,
                          title="Show", original_title="Show", names=["Show"])
        monkeypatch.setattr(MediaChain, "async_recognize_media", AsyncMock(return_value=media))
        monkeypatch.setattr(MediaChain, "async_supplement_media_info", AsyncMock(return_value=media))
        params = {"media_source": MediaSource.TMDB, "media_id": "1", "mtype": MediaType.TV}
    stream = SearchManualOwner.events(owner, params=params)
    if transport:
        from app.api.endpoints import search as search_endpoint

        monkeypatch.setattr(search_endpoint, "_SSE_APPEND_FLUSH_INTERVAL", 0.01)
        stream = search_endpoint._iter_batched_search_events(stream)
    received = []
    try:
        async with asyncio.timeout(3):
            while True:
                event = await anext(stream)
                received.append(event)
                if event.get("items"):
                    break
        assert started.is_set() and not finished.is_set()
        assert event["type"] == "append" and event["stage"] == "searching"
        assert [item["torrent_info"]["site"] for item in event["items"]] == [2]
        assert "sources" not in event
        assert not any(item["type"] in {"replace", "done"} for item in received)
        assert saved == []
        if media_search:
            assert event["items"][0]["match_status"] == "candidate"
            assert event["items"][0]["media_info_is_target"] is False
        release.set()
        received.extend(await events(stream))
    finally:
        release.set()
        await stream.aclose()
    assert sorted(calls) == [(1, "Show", 0), (2, "Show", 0)]
    assert [item["type"] for item in received[-2:]] == ["replace", "done"]
    assert len(received[-2]["items"]) == 2
    assert [item["site_name"] for item in site_facts(received)] == ["A", "B"]
    assert saved and not registry.records


@pytest.mark.asyncio
async def test_closing_preview_waits_for_inflight_workers_without_committing_page(manual_owner, blocked_site):
    """离开搜索流时等待在途同步请求收尾，不写最终结果和翻页状态。"""
    owner, _, (_, saved) = manual_owner
    started, release, finished, registry = blocked_site
    stream = SearchManualOwner.events(owner, params={"keyword": "Show"})
    try:
        async with asyncio.timeout(3):
            while not (await anext(stream)).get("items"):
                pass
        assert started.is_set() and not finished.is_set()
        closing = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, timeout=3)
        assert finished.is_set() and not registry.records
        assert saved == []
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(None, None), (MediaType.UNKNOWN, None),
                                               (MediaType.TV, MediaType.TV), (MediaType.MOVIE, MediaType.MOVIE)])
async def test_title_search_keeps_unrestricted_mteam_categories_and_explicit_type(manual_owner, monkeypatch, requested, expected):
    from app.modules.indexer import IndexerModule
    from app.modules.indexer.spider import mtorrent as mtorrent_module
    from app.modules.indexer.spider.mtorrent import MTorrentSpider

    owner, _, _ = manual_owner
    monkeypatch.setattr(mtorrent_module, "get_configured_system_config", lambda: None)
    spider = MTorrentSpider({"id": 1, "name": "M-Team", "domain": "https://mteam.example/", "apikey": "fake"})
    requests = []
    plugin_types = []

    class FakeRequest:
        """回放 M-Team 的分类与分页响应，不访问真实站点。"""

        def __init__(self, **_params):
            """接受原传输层参数以保持索引器调用协议。"""
            pass

        def post_res(self, _url=None, json=None, **_params):
            """用分类和页号生成可重复的离线搜索响应。"""
            requests.append(json)
            count = 1 if json["categories"] == MTorrentSpider._movie_category else 3
            offset = (json["pageNumber"] - 1) * count
            return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {
                "data": [{"id": str(index), "name": f"Show S01E{index:02d}", "category": "403",
                          "size": "1024", "status": {}} for index in range(offset + 1, offset + count + 1)],
                "pageNumber": json["pageNumber"], "pageSize": 100, "totalPages": 2,
                "total": count * 2}})

    monkeypatch.setattr(mtorrent_module, "RequestUtils", FakeRequest)
    monkeypatch.setattr(owner, "_sync_indexers", lambda _ids: [{"id": 1, "name": "M-Team"}])
    def request(**params):
        assert params["mtype"] == expected
        error, torrents = spider.search(keyword=params["keyword"], mtype=params["mtype"], page=params["page"])
        assert not error
        report_site_search_outcome(attempted=True, outcome="success")
        return IndexerModule._IndexerModule__parse_result(params["site"], torrents, 0)
    monkeypatch.setattr(owner, "search_site_torrents", request)
    monkeypatch.setattr(owner, "search_plugin_torrents", lambda **params: plugin_types.append(params["mtype"]) or [])
    params = {"keyword": "Show"}
    if requested is not None:
        params["mtype"] = requested
    result = await events(SearchManualOwner.events(owner, params=params))
    expected_categories = ([] if expected is None else MTorrentSpider._tv_category
                           if expected == MediaType.TV else MTorrentSpider._movie_category)
    assert requests[0]["categories"] == expected_categories
    assert len(result[-2]["items"]) == (1 if expected == MediaType.MOVIE else 3)
    assert not any(source["error"] for source in result[-1]["sources"])
    assert plugin_types == [expected]
    source = result[-1]["sources"][0]
    next_page = await events(SearchManualOwner.events(owner, params={
        **params, "page": source["page"] + 1, "source": source["source"]}))
    assert [request["pageNumber"] for request in requests] == [1, 2]
    assert all(request["categories"] == expected_categories for request in requests)
    assert len(next_page[-2]["items"]) == (1 if expected == MediaType.MOVIE else 3)
    assert not any(source["error"] for source in next_page[-1]["sources"])
    assert plugin_types == [expected]


@pytest.mark.asyncio
async def test_first_request_loads_page_zero_of_every_source_and_saves_last_search(manual_owner):
    """首页并发请求各来源，但完成页事实仍保持来源顺序。"""
    owner, calls, (_, saved) = manual_owner
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show"}))
    assert sorted(calls) == [(1, "Show", 0), (2, "Show", 0)]
    sources = site_facts(result)
    assert [(item["site_name"], item["page"], item["can_continue"], item["error"]) for item in sources] == [
        ("A", 0, True, None), ("B", 0, True, None)]
    assert decode_source(sources[0]["source"])[:2] == ("1", "Show")
    assert set(sources[0]) == {"source", "site_name", "page", "can_continue", "error"}
    plugin = next(item for item in result[-1]["sources"] if decode_source(item["source"])[0] == "plugin")
    assert not plugin["can_continue"]
    assert len(result[-2]["items"]) == 2
    assert saved


@pytest.mark.asyncio
async def test_continuation_restores_previous_page_and_keeps_last_search(manual_owner):
    owner, calls, (_, saved) = manual_owner
    result = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 1, "source": encode_source("2", "Show")}))
    assert calls == [(2, "Show", 0), (2, "Show", 1)]
    assert [item["torrent_info"]["description"] for item in result[-2]["items"]] == ["2-1"]
    assert [(item["page"], item["can_continue"]) for item in result[-1]["sources"]] == [(1, True)]
    # 续页不能把上次搜索缓存覆盖为单站单页。
    assert saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pageable", [True, False])
async def test_failed_source_keeps_its_page_for_retry_without_hiding_other_sources(manual_owner, monkeypatch, pageable):
    """来源失败不影响其他来源，重试只请求原失败页。"""
    owner, calls, (failures, _) = manual_owner
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: 100 if pageable else None)
    failures.add((1, 0))
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show"}))
    first, second = site_facts(result)
    assert (first["error"], first["page"], first["can_continue"]) == ("timeout", 0, True)
    assert (second["error"], second["page"], second["can_continue"]) == (None, 0, pageable)
    assert sorted(calls) == [(1, "Show", 0), (2, "Show", 0)]
    failures.clear()
    retried = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": first["page"], "source": first["source"]}))
    assert sorted(calls) == [(1, "Show", 0), (1, "Show", 0), (2, "Show", 0)]
    assert (site_facts(retried)[0]["error"], site_facts(retried)[0]["can_continue"]) == (None, pageable)


@pytest.mark.asyncio
async def test_alias_fallback_only_on_first_page_and_source_keeps_actual_keyword(manual_owner, monkeypatch):
    owner, calls, (failures, _) = manual_owner
    monkeypatch.setattr(owner, "_prepare_params", lambda **_params: (None, ["AliasA", "AliasB"]))
    failures.update({(1, 0), (2, 0)})
    await events(SearchManualOwner.events(owner, params={"keyword": "Show"}))
    assert [call[1] for call in calls] == ["AliasA", "AliasA", "AliasB", "AliasB"]
    calls.clear()
    await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 3, "source": encode_source("1", "AliasB")}))
    assert calls == [(1, "AliasB", 2), (1, "AliasB", 3)]


@pytest.mark.asyncio
async def test_zero_display_rows_still_have_more(manual_owner, monkeypatch):
    """标题预览和最终结果都为空时，原始页事实仍允许继续加载。"""
    from app.chain.search.manual import _ManualPage

    owner, _, _ = manual_owner
    # 过滤规则把本页结果全部过滤掉：显示为空，但站点仍有后续页。
    monkeypatch.setattr(_ManualPage, "results", lambda _self, _torrents=None: [])
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "sites": [1]}))
    assert result[-2]["items"] == []
    assert all(not item["items"] for item in result if item["type"] == "append")
    assert all(item["can_continue"] for item in site_facts(result))


@pytest.mark.asyncio
async def test_empty_raw_page_has_no_more(manual_owner, monkeypatch):
    owner, _, _ = manual_owner
    def request(**_params):
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=0)
        return []
    monkeypatch.setattr(owner, "search_site_torrents", request)
    result = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 4, "source": encode_source("1", "Show")}))
    assert [(item["page"], item["can_continue"]) for item in result[-1]["sources"]] == [(4, False)]


@pytest.mark.asyncio
async def test_unidentifiable_resource_does_not_fail_the_page(manual_owner, monkeypatch):
    owner, _, _ = manual_owner
    def request(**params):
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=2, has_more=True)
        return [TorrentInfo(site=params["site"]["id"], description="x", title=""),
                TorrentInfo(site=params["site"]["id"], description="y", title="Show S01E01")]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "sites": [1]}))
    assert result[-1]["sources"][0]["error"] is None


@pytest.mark.asyncio
async def test_site_without_pagination_has_only_the_first_page(manual_owner, monkeypatch):
    owner, calls, _ = manual_owner
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: None)
    first = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "sites": [1]}))
    assert not site_facts(first)[0]["can_continue"]
    calls.clear()
    later = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 1, "source": encode_source("1", "Show")}))
    assert calls == []
    assert not later[-1]["sources"][0]["can_continue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_all", [False, True])
async def test_raw_page_signature_stops_only_the_identical_adjacent_page(manual_owner, monkeypatch, filter_all):
    """原始页摘要独立于展示过滤，仅相邻页完全重复时结束来源。"""
    from app.chain.search.manual import _ManualPage

    owner, calls, _ = manual_owner
    def request(**params):
        page = params["page"]
        calls.append(page)
        numbers = [1, 2] if page == 0 else [1, 3]
        report_site_search_outcome(attempted=True, outcome="success")
        # 站点不给总页数：不能为了确定还有没有结果而预取后页。
        report_site_search_page(raw_count=len(numbers))
        return [TorrentInfo(site=1, site_name="A", title=f"Show S01E{number:02d}", description=str(number))
                for number in numbers]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    if filter_all:
        monkeypatch.setattr(_ManualPage, "results", lambda _self, _torrents=None: [])
    first = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "sites": [1]}))
    first_source = site_facts(first)[0]
    assert calls == [0]
    assert first_source["can_continue"]
    second = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 1, "source": first_source["source"]}))
    second_source = site_facts(second)[0]
    assert calls == [0, 1]
    assert second_source["can_continue"]
    assert len(second[-2]["items"]) == (0 if filter_all else 2)
    repeated = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 2, "source": second_source["source"]}))
    assert calls == [0, 1, 2]
    assert repeated[-2]["items"] == []
    assert not site_facts(repeated)[0]["can_continue"]


@pytest.mark.asyncio
async def test_pages_beyond_the_cap_are_not_requested(manual_owner):
    owner, calls, _ = manual_owner
    result = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 100, "source": encode_source("1", "Show")}))
    assert calls == []
    assert not result[-1]["sources"][0]["can_continue"]
    await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 99, "source": encode_source("1", "Show")}))
    assert calls == [(1, "Show", 98), (1, "Show", 99)]


@pytest.mark.asyncio
async def test_invalid_source_is_rejected(manual_owner):
    owner, calls, _ = manual_owner
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 1, "source": "bad!"}))
    assert result == [{"type": "error", "message": "搜索来源无效，请重新搜索"}]
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("season,expected", [(None, [0, 1, 2]), (0, [0]), (1, [1]), (2, [2])])
async def test_media_page_keeps_original_season_filter(manual_owner, monkeypatch, season, expected):
    """手动精确搜索复用原季范围；过滤后的条数不改变原始页事实。"""
    from unittest.mock import AsyncMock

    from app.chain.media import MediaChain
    from app.domain.context import MediaInfo
    from app.schemas.types import MediaSource

    owner, _, _ = manual_owner
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.TV,
                      title="Example Show", original_title="Example Show", names=["Example Show"])
    monkeypatch.setattr(MediaChain, "async_recognize_media", AsyncMock(return_value=media))
    monkeypatch.setattr(MediaChain, "async_supplement_media_info", AsyncMock(return_value=media))
    def request(**_params):
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=3, has_more=True)
        return [TorrentInfo(site=1, site_name="A", title=f"Example Show S0{number}E01",
                            description=f"season {number}", enclosure=f"https://site.example/download?id={number}")
                for number in range(3)]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    params = {"media_source": MediaSource.TMDB, "media_id": "1", "mtype": MediaType.TV, "sites": [1],
              "season": season}
    result = await events(SearchManualOwner.events(owner, params=params))
    assert sorted(item["meta_info"]["begin_season"] for item in result[-2]["items"]) == expected
    assert site_facts(result)[0]["can_continue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed,expected", [(36 * 3600 - 1, [3]), (36 * 3600 + 1, [2, 3])])
async def test_previous_page_cache_expires_after_thirty_six_hours(manual_owner, page_clock, elapsed, expected):
    owner, calls, (_, saved) = manual_owner
    source = encode_source("1", "Show")
    await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 2, "source": source}))
    calls.clear()
    page_clock[0] += elapsed
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 3, "source": source}))
    assert [page for _site, _keyword, page in calls] == expected
    assert [item["torrent_info"]["description"] for item in result[-2]["items"]] == ["1-3"]
    assert result[-1]["sources"][0]["page"] == 3
    assert "signature" not in result[-1]["sources"][0]
    assert saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_page,retried_pages", [(2, [2, 3]), (3, [3])])
async def test_expired_page_recovery_keeps_target_on_timeout(manual_owner, failed_page, retried_pages):
    owner, calls, (failures, _) = manual_owner
    source = encode_source("1", "Show")
    failures.add((1, failed_page))
    failed = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 3, "source": source}))
    assert [page for _site, _keyword, page in calls] == ([2] if failed_page == 2 else [2, 3])
    assert failed[-2]["items"] == []
    assert failed[-1]["sources"][0] == {
        "source": source, "site_name": "A", "page": 3, "can_continue": True, "error": "timeout"}
    failures.clear()
    calls.clear()
    retried = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 3, "source": source}))
    assert [page for _site, _keyword, page in calls] == retried_pages
    assert [item["torrent_info"]["description"] for item in retried[-2]["items"]] == ["1-3"]


@pytest.mark.asyncio
async def test_refreshed_previous_page_drives_duplicate_detection(manual_owner, monkeypatch, page_clock):
    owner, calls, _ = manual_owner
    source = encode_source("1", "Show")
    await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 2, "source": source}))
    page_clock[0] += 36 * 3600 + 1
    calls.clear()
    def request(**params):
        calls.append(params["page"])
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1)
        return [TorrentInfo(site=1, site_name="A", title="Show S01E09", description="fresh-last-page")]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    result = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "page": 3, "source": source}))
    assert calls == [2, 3]
    assert result[-2]["items"] == []
    assert not result[-1]["sources"][0]["can_continue"]


@pytest.mark.asyncio
async def test_same_page_retry_compares_with_previous_page_not_itself(manual_owner):
    owner, calls, _ = manual_owner
    first = await events(SearchManualOwner.events(owner, params={"keyword": "Show", "sites": [1]}))
    source = site_facts(first)[0]["source"]
    params = {"keyword": "Show", "page": 1, "source": source}
    second = await events(SearchManualOwner.events(owner, params=params))
    retried = await events(SearchManualOwner.events(owner, params=params))
    assert [page for _site, _keyword, page in calls] == [0, 1, 1]
    assert retried[-2]["items"] == second[-2]["items"]
    assert len(retried[-2]["items"]) == 1
    assert site_facts(retried)[0]["can_continue"]


@pytest.mark.asyncio
async def test_search_rounds_do_not_share_previous_page(manual_owner, monkeypatch):
    owner, _, _ = manual_owner
    first_pages = iter(["A", "B"])
    def request(**params):
        description = next(first_pages) if params["page"] == 0 else "B"
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1)
        return [TorrentInfo(site=1, site_name="A", title="Show S01E01", description=description)]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    params = {"keyword": "Show", "sites": [1]}
    first = await events(SearchManualOwner.events(owner, params=params))
    other = await events(SearchManualOwner.events(owner, params=params))
    first_source, other_source = site_facts(first)[0]["source"], site_facts(other)[0]["source"]
    assert first_source != other_source
    continued = await events(SearchManualOwner.events(owner, params={**params, "page": 1, "source": first_source}))
    repeated = await events(SearchManualOwner.events(owner, params={**params, "page": 1, "source": other_source}))
    assert len(continued[-2]["items"]) == 1 and site_facts(continued)[0]["can_continue"]
    assert repeated[-2]["items"] == [] and not site_facts(repeated)[0]["can_continue"]


@pytest.mark.asyncio
async def test_recovery_requests_both_pages_through_existing_site_interval(manual_owner, monkeypatch, page_clock):
    from app.chain.search import provider

    owner, calls, _ = manual_owner
    monkeypatch.setattr(owner, "_sync_indexers", lambda _ids: [{"id": 1, "name": "A", "limit_seconds": 10}])
    waits = []
    def sleep(delay):
        waits.append(delay)
        page_clock[0] += delay
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: page_clock[0], sleep=sleep))
    monkeypatch.setattr(provider, "_site_next_request_at", {})
    result = await events(SearchManualOwner.events(owner, params={
        "keyword": "Show", "page": 3, "source": encode_source("1", "Show")}))
    assert [page for _site, _keyword, page in calls] == [2, 3]
    assert waits == [10]
    assert [item["torrent_info"]["description"] for item in result[-2]["items"]] == ["1-3"]
