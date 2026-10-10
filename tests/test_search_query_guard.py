"""IMDb 异常来源早停、跨来源提交屏障及恢复前史的离线回归。"""

from dataclasses import replace
from typing import Optional

import pytest

from app.application.search.session import (
    SearchSessionSnapshot,
    collection_snapshot,
    encode_search_state,
    restore_collection,
)
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.chain.search.execution import MediaSearchPlan
from app.chain.search.facade import SearchChain
from app.chain.search.result import TorrentHelper
from app.chain.search.scan import ScanPage, SearchScan
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.search import SearchCollection, SearchResourceEvidence, SearchSourceCursor
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def search(monkeypatch):
    """保留真实逐页识别与收集，仅替换站点请求、同名消歧及排序的外部边界。"""
    owner = SearchChain()
    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(owner, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(TorrentHelper, "match_torrent", staticmethod(
        lambda torrent, **_params: bool(torrent.title and torrent.title.startswith("Movie"))))
    monkeypatch.setattr(TorrentHelper, "requires_identity_disambiguation", staticmethod(lambda **_params: False))
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda items: items))
    data, calls = {}, []

    def request(**params):
        """回放指定站点页，记录真实请求顺序并报告站点成功页事实。"""
        site, page = params["site"]["id"], params["page"]
        calls.append((site, page))
        items = data.get((site, page), [])
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items), has_more=bool(items))
        return items

    monkeypatch.setattr(owner, "search_site_torrents", request)
    return owner, data, calls


def movie_plan() -> MediaSearchPlan:
    """构造普通电影订阅的 IMDb 精确查询，不触发详情补全。"""
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, imdb_id="tt4633694",
                      type=MediaType.MOVIE, title="Movie", names=["Movie"], year="2026")
    return MediaSearchPlan(media, area="imdbid", rule_groups=[])


def resource(site: int, page: int, *, matched: bool = False, title: Optional[str] = None) -> TorrentInfo:
    """构造不同页面的明确作品身份，避免重复页防护掩盖异常查询早停。"""
    return TorrentInfo(site=site, site_name=str(site), description=f"page-{page}",
                       title=title if title is not None else f"{'Movie' if matched else 'Other'} {page + 2000} 1080p",
                       media_source=MediaSource.IMDb, media_id="tt4633694" if matched else "tt9999999",
                       enclosure=f"https://site.example/download?id={site}{page}")


def checkpoint(scan: SearchScan) -> SearchSessionSnapshot:
    """模拟队列让出或进程退出后的 JSON 检查点，不依赖原内存对象。"""
    return SearchSessionSnapshot("task", 1, encode_search_state(scan.state()))


def test_third_unmatched_page_releases_other_site_candidate(search):
    """异常站点恰好请求 3 页，随后提交另一站点早已匹配的电影，而不是等待 20 页。"""
    owner, data, calls = search
    data[1, 0] = [resource(1, 0, matched=True)]
    data.update({(2, page): [resource(2, page)] for page in range(20)})
    scan = SearchScan(owner=owner, plan=movie_plan())
    batches = []

    def submit(items, ready):
        """记录提交发生时的请求边界，确认尚未请求异常来源第 4 页。"""
        batches.append((list(calls), [item.torrent_info.site for item in items]))
        return ready

    scan.run(submit)
    assert batches == [([(1, 0), (2, 0), (1, 1), (2, 1), (2, 2)], [1])]
    source = scan.collection.sources["2:tt4633694"]
    assert source.unmatched_pages == 3 and source.limit_reached
    assert not source.exhausted and not source.failed
    assert scan.ended and not scan.collection.remaining


def test_unknown_page_breaks_unmatched_streak_before_late_match(search):
    """未知页打断连续计数，第 6 页中的精确资源仍能提交。"""
    owner, data, calls = search
    data.update({(2, page): [resource(2, page)] for page in range(5)})
    data[2, 2] = [resource(2, 2, title="")]
    data[2, 5] = [resource(2, 5, matched=True)]
    scan = SearchScan(owner=owner, plan=movie_plan())
    scan.run(lambda _items, ready: ready)
    assert [page for site, page in calls if site == 2] == list(range(7))
    assert scan.collection.sources["2:tt4633694"].matched_work
    assert not scan.collection.remaining


@pytest.mark.parametrize("mode", ["title", "full", "whole_season", "fallback"])
def test_other_search_modes_keep_late_candidates(search, mode):
    """名称搜索、全量、整季及深度兜底均保留第 5 页的有效候选。"""
    owner, data, calls = search
    plan = movie_plan()
    if mode == "title":
        plan = replace(plan, area="title")
    if mode == "whole_season":
        plan.mediainfo.type, plan.mediainfo.season = MediaType.TV, 1
        plan = replace(plan, no_exists={"tmdb:1": {1: NotExistMediaInfo(episodes=[])}})
    data.update({(2, page): [resource(2, page)] for page in range(4)})
    data[2, 4] = [resource(2, 4, matched=True, title="Movie S01 2026 1080p")]
    scan = SearchScan(owner=owner, plan=plan, full=mode == "full")
    if mode == "fallback":
        scan.collection.deepen({"movie"})
    scan.run(lambda _items, ready: ready)
    assert [page for site, page in calls if site == 2] == list(range(6))
    assert not scan.collection.remaining


def test_unmatched_count_survives_checkpoint(search):
    """恢复检查点后第 3 页即停止，不重新从零累计异常页。"""
    owner, data, calls = search
    data.update({(2, page): [resource(2, page)] for page in range(20)})
    scan = SearchScan(owner=owner, plan=movie_plan())
    scan.step("2:tt4633694")
    scan.step("2:tt4633694")
    restored = SearchScan(owner=owner, plan=movie_plan(), snapshot=checkpoint(scan))
    assert restored.collection.sources["2:tt4633694"].unmatched_pages == 2
    restored.run(lambda _items, ready: ready)
    assert [page for site, page in calls if site == 2] == [0, 1, 2]
    assert restored.ended


def test_other_season_match_survives_checkpoint_without_current_candidates(search):
    """同作品其他季即使不覆盖当前缺集、未保留候选，也会永久解除该来源的异常早停。"""
    owner, data, calls = search
    plan = movie_plan()
    plan.mediainfo.type, plan.mediainfo.season = MediaType.TV, 2
    plan = replace(plan, no_exists={"tmdb:1": {2: NotExistMediaInfo(episodes=[1])}})
    data[2, 0] = [resource(2, 0, matched=True, title="Movie S01E03 2026 1080p")]
    data.update({(2, page): [resource(2, page)] for page in range(1, 4)})
    data[2, 4] = [resource(2, 4, matched=True, title="Movie S02E01 2026 1080p")]
    scan = SearchScan(owner=owner, plan=plan)
    scan.step("2:tt4633694")
    assert not scan.state()["candidates"]
    restored = SearchScan(owner=owner, plan=plan, snapshot=checkpoint(scan))
    assert restored.collection.sources["2:tt4633694"].matched_work
    restored.run(lambda _items, ready: ready)
    assert [page for site, page in calls if site == 2] == list(range(6))
    assert not restored.collection.remaining


def test_deferred_page_does_not_count_as_unmatched(search, monkeypatch):
    """冷却只保留原页和计数，不作为第 3 个明确不匹配页。"""
    from app.application.site.observation import SiteSearchObservation

    owner, data, _calls = search
    data.update({(2, page): [resource(2, page)] for page in range(2)})
    scan = SearchScan(owner=owner, plan=movie_plan())
    source_key = "2:tt4633694"
    scan.step(source_key)
    scan.step(source_key)
    monkeypatch.setattr(scan.sources, "fetch", lambda *_args, **_kwargs: ScanPage(
        [], SiteSearchObservation(), "2999-01-01T00:00:00+00:00", "cooldown"))
    assert not scan.step(source_key)
    source = scan.collection.sources[source_key]
    assert source.next_page == 2 and source.unmatched_pages == 2
    assert not source.limit_reached


def test_head_refresh_does_not_count_but_a_match_disables_guard():
    """补拉首页不拼接成第 3 个连续不匹配页，首页命中作品后也不会再早停。"""
    source = SearchSourceCursor(next_page=2, unmatched_pages=2, head_refresh=True)
    source.accept_page(page=0, evidence=[SearchResourceEvidence("other", frozenset())], targets={"movie"},
                       exhausted=False, now=1, stop_unmatched=True)
    assert source.unmatched_pages == 2 and not source.limit_reached
    source.head_refresh = True
    source.accept_page(page=0, evidence=[SearchResourceEvidence("movie", frozenset({"movie"}))],
                       targets={"movie"}, exhausted=False, now=2, stop_unmatched=True)
    assert source.matched_work and source.unmatched_pages == 0


def test_legacy_cursor_without_match_history_only_uses_page_cap():
    """旧断点无法证明前史零命中，仍在第 20 页关闭并释放候选收集屏障。"""
    state = collection_snapshot(SearchCollection({"movie"}, {"site": SearchSourceCursor(next_page=19)}))
    state["sources"]["site"].pop("matched_work")
    state["sources"]["site"].pop("unmatched_pages")
    restored = restore_collection(state)
    source = restored.sources["site"]
    assert source.matched_work
    source.accept_page(page=19, evidence=[SearchResourceEvidence("other", frozenset())], targets={"movie"},
                       exhausted=False, now=1, stop_unmatched=True)
    assert source.unmatched_pages == 0 and source.limit_reached
    assert restored.ready() == {"movie"}


def test_query_area_change_restarts_instead_of_reusing_early_stop(search):
    """IMDb 改为名称查询后重新建立游标，不能沿用旧来源的异常停止证据。"""
    owner, data, _calls = search
    data.update({(2, page): [resource(2, page)] for page in range(2)})
    plan = movie_plan()
    scan = SearchScan(owner=owner, plan=plan)
    scan.step("2:tt4633694")
    scan.step("2:tt4633694")
    restored = SearchScan(owner=owner, plan=replace(plan, area="title"), snapshot=checkpoint(scan))
    assert restored.restart
    restored.run(lambda _items, ready: ready)
    assert "2:Movie" in restored.collection.sources
    assert "2:tt4633694" not in restored.collection.sources


def test_v2_checkpoint_keeps_settled_goals_and_conservative_progress(search):
    """可读取的 V2 JSON 缺少匹配前史时保守恢复，已提交目标不丢失，新快照写 V3。"""
    owner, _data, _calls = search
    plan = movie_plan()
    scan = SearchScan(owner=owner, plan=plan)
    source = scan.collection.sources["2:tt4633694"]
    source.next_page = 19
    scan.collection.settle({"movie"})
    state = scan.state()
    state["format"] = 2
    state["contract"] = scan._contract_signature(include_area=False)
    for cursor in state["collection"]["sources"].values():
        cursor.pop("matched_work")
        cursor.pop("unmatched_pages")
    snapshot = SearchSessionSnapshot("task", 1, encode_search_state(state))
    restored = SearchScan(owner=owner, plan=plan, snapshot=snapshot)
    assert not restored.restart
    assert restored.collection.settled == {"movie"}
    assert restored.collection.sources["2:tt4633694"].matched_work
    assert restored.collection.sources["2:tt4633694"].next_page == 19
    assert restored.state()["format"] == 3


def test_v2_cursor_already_beyond_twenty_pages_submits_cached_candidate_without_requests(search):
    """升级前已经翻过 20 页的异常来源直接释放屏障，不重扫旧页或丢掉其他站点候选。"""
    owner, data, calls = search
    plan = movie_plan()
    data[1, 0] = [resource(1, 0, matched=True)]
    scan = SearchScan(owner=owner, plan=plan)
    scan.step("1:tt4633694")
    scan.step("1:tt4633694")
    scan.step("plugin:tt4633694")
    scan.collection.sources["2:tt4633694"].next_page = 50
    state = scan.state()
    state["format"] = 2
    state["contract"] = scan._contract_signature(include_area=False)
    for cursor in state["collection"]["sources"].values():
        cursor.pop("matched_work")
        cursor.pop("unmatched_pages")
    restored = SearchScan(owner=owner, plan=plan,
                          snapshot=SearchSessionSnapshot("task", 1, encode_search_state(state)))
    assert not restored.restart
    calls.clear()
    submitted = []
    restored.run(lambda items, ready: submitted.extend(item.torrent_info.site for item in items) or ready)
    assert submitted == [1]
    assert calls == []
    assert restored.ended and not restored.collection.remaining
