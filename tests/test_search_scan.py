"""
完整页驱动验证实际补集、过滤兜底和逐页识别成本。
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.application.search.session import SearchSessionSnapshot, encode_search_state
from app.application.site.observation import SiteSearchObservation, report_site_search_outcome, report_site_search_page
from app.application.subscription.sitebudget import (
    SiteBudgetClaim,
    SubscriptionSearchDeferred,
    SubscriptionSiteBudget,
    SubscriptionSiteBudgetUnavailable,
)
from app.chain.search import scan as scan_module
from app.chain.search.execution import MediaSearchPlan
from app.chain.search.facade import SearchChain
from app.chain.search.result import TorrentHelper
from app.chain.search.scan import (
    AUTOMATIC_REQUEST_INTERVAL,
    BUDGET_DEFERRAL_LIMIT,
    BUDGET_DEFERRAL_SECONDS,
    ScanPage,
    SearchScan,
    SearchSources,
)
from app.domain.context import MediaInfo, TorrentInfo
from app.runtime.stop import runtime_stop_state
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def owner(monkeypatch):
    """
    构造已隔离站点、插件和过滤依赖的搜索链。
    """
    chain = SearchChain()
    monkeypatch.setattr(chain, "_sync_indexers", lambda _sites: [{"id": 1, "name": "test", "parser": "mTorrent"}])
    monkeypatch.setattr(chain, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(chain, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(TorrentHelper, "match_torrent", staticmethod(lambda **_params: True))
    monkeypatch.setattr(TorrentHelper, "requires_identity_disambiguation", staticmethod(lambda **_params: False))
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda items: items))
    return chain


def plan(episodes):
    """
    为指定缺集编号创建固定剧集身份的搜索计划。
    """
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.TV,
                      title="Show", original_title="Show", names=["Show"], season=1, year="2026")
    return MediaSearchPlan(media, keyword="Show", no_exists={"tmdb:1": {1: NotExistMediaInfo(episodes=episodes)}},
                           rule_groups=[])


def torrent(episode, version=1):
    """
    按集数和资源版本构造可区分的站点种子候选。
    """
    return TorrentInfo(site=1, description=f"{episode}-{version}", title=f"Show S01E{episode:02d} 2026 1080p",
                       enclosure=f"https://site.example/download?id={episode}{version}")


def pages(owner, monkeypatch, data):
    """
    用内存页面替换站点请求，并返回请求页码记录。
    """
    calls = []
    def request(**params):
        page = params["page"]
        calls.append(page)
        values = data.get(page, [])
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(values))
        return values
    monkeypatch.setattr(owner, "search_site_torrents", request)
    return calls


def test_thirty_seven_episodes_fill_older_pages_and_download_early(owner, monkeypatch):
    """
    多页搜索补齐缺集，同时在较早目标具备候选时提前提交下载。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(ep, v) for ep in range(16, 38) for v in range(1, 4)],
                                     1: [torrent(ep) for ep in range(1, 16)]})
    batches = []
    scan = SearchScan(owner=owner, plan=plan(list(range(1, 38))))
    def submit(contexts, ready):
        batches.append((calls[-1], set(ready)))
        return ready
    scan.run(submit)
    assert calls == [0, 1, 2]
    assert batches[0] == (1, {f"1:{ep}" for ep in range(16, 38)})
    assert not scan.collection.remaining


@pytest.mark.parametrize("download_success", [False, True])
def test_single_page_policy_does_not_expand_after_missing_or_failed_download(owner, monkeypatch, download_success):
    """
    单页策略在缺集或下载失败后仍不得扩展到下一页。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(2)], 1: [torrent(1)]})
    scan = SearchScan(owner=owner, plan=plan([1, 2]), page_limit=1)
    submitted = []
    def submit(items, ready):
        submitted.append([item.torrent_info.description for item in items])
        return ready if download_success else set()
    scan.run(submit)
    assert calls == [0]
    assert submitted == [["2-1"]]
    assert scan.collection.remaining == ({"1:1"} if download_success else {"1:1", "1:2"})
    source = scan.collection.sources["1:Show"]
    assert source.limit_reached
    assert not source.exhausted
    assert scan.ended and "本轮搜索结束" in scan.progress_text()


def test_single_page_policy_keeps_each_alias_first_page(owner, monkeypatch):
    """
    单页策略允许逐个别名搜索首页，但不向后分页。
    """
    monkeypatch.setattr(owner, "_prepare_params", lambda **_params: (None, ["First", "Alias"]))
    calls = []
    def request(**params):
        calls.append((params["keyword"], params["page"]))
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1, has_more=True)
        return [torrent(9)] if params["keyword"] == "First" else [torrent(1)]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1]), page_limit=1)
    scan.run(lambda _items, ready: ready)
    assert calls == [("First", 0), ("Alias", 0)]
    assert not scan.collection.remaining


def test_single_page_filter_rejection_does_not_request_more_pages(owner, monkeypatch):
    """
    单页候选全部被过滤时仍不请求后续页面。
    """
    from dataclasses import replace

    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(1, 2)]})
    search_plan = replace(plan([1]), candidate_filter=lambda _items: [])
    scan = SearchScan(owner=owner, plan=search_plan, page_limit=1)
    scan.run(lambda _items, _ready: pytest.fail("过滤后不应提交下载"))
    assert calls == [0]
    assert scan.collection.remaining == {"1:1"}
    assert scan.collection.fallback == {"1:1"}
    assert not scan.collection.active_sources()


def test_single_page_waits_for_other_sites_before_choosing(owner, monkeypatch):
    """
    单页策略等各站点首页返回后再选择下载候选。
    """
    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [{"id": 1}, {"id": 2}])
    calls = []
    def request(**params):
        calls.append((params["site"]["id"], params["page"]))
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1, has_more=True)
        item = torrent(1, params["site"]["id"])
        item.site = params["site"]["id"]
        return [item]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1]), page_limit=1)
    submitted = []
    def submit(items, ready):
        submitted.append((list(calls), {item.torrent_info.site for item in items}))
        return ready
    scan.run(submit)
    assert submitted == [([(1, 0), (2, 0)], {1, 2})]


def test_single_page_limit_survives_checkpoint_before_download(owner, monkeypatch):
    """
    下载前保存再恢复检查点后，单页限制仍然生效。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(2)]})
    scan = SearchScan(owner=owner, plan=plan([1, 2]), page_limit=1)
    for key in list(scan.collection.sources):
        scan.step(key)
    snapshot = SearchSessionSnapshot("single", 1, encode_search_state(scan.state()))
    restored = SearchScan(owner=owner, plan=plan([1, 2]), page_limit=1, snapshot=snapshot)
    restored.run(lambda _items, ready: ready)
    assert calls == [0]
    assert restored.collection.remaining == {"1:2"}
    assert restored.collection.sources["1:Show"].limit_reached


@pytest.mark.parametrize("full,expected", [(False, [0, 1]), (True, [0, 1, 2, 3])])
def test_movie_smart_boundary_and_complete_upgrade_search(owner, monkeypatch, full, expected):
    """
    电影智能搜索按覆盖边界停止，完整搜索继续收集后续升级候选。
    """
    from app.application.subscription.contract import SubscriptionSnapshot
    from app.chain.download import DownloadChain
    from app.chain.subscribe.facade import SubscribeChain

    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.MOVIE,
                      title="Movie", names=["Movie"], year="2026")
    movie_plan = MediaSearchPlan(media, keyword="Movie", rule_groups=[])
    monkeypatch.setattr(TorrentHelper, "match_torrent", staticmethod(lambda torrent, **_params: torrent.title.startswith("Movie")))
    calls = pages(owner, monkeypatch, {
        0: [TorrentInfo(site=1, description="movie-first", title="Movie 2026 1080p", enclosure="https://site.example/download?id=1")],
        1: [torrent(9)],
        2: [TorrentInfo(site=1, description="movie-later", title="Movie 2026 2160p", enclosure="https://site.example/download?id=2")],
    })
    submitted = []
    subscription_owner = SubscribeChain.__new__(SubscribeChain)
    monkeypatch.setattr(subscription_owner, "subscription_repository", None, raising=False)
    subscribe = SubscriptionSnapshot(id=1, name="Movie", type=MediaType.MOVIE.value, best_version=int(full))
    monkeypatch.setattr(DownloadChain, "batch_download", lambda _self, **params: (submitted.extend(
        item.torrent_info.description for item in params["contexts"]) or params["contexts"], {}))
    def submit(items, ready):
        downloads, _ = subscription_owner._SubscribeChain__download_best_version_with_full_pack_first(
            contexts=items, no_exists={}, subscribe=subscribe, mediakey="tmdb:1", eligible_targets=ready)
        return ready if downloads else set()
    scan = SearchScan(owner=owner, plan=movie_plan, full=full)
    scan.run(submit)
    assert calls == expected
    assert submitted == (["movie-first", "movie-later"] if full else ["movie-first"])


def test_whole_season_uses_trusted_range_or_complete_scan(owner):
    """
    整季展开为真实集号后仍保留完整搜索，未知集数沿用整季目标。
    """
    from app.chain.search.scan import search_targets
    season_plan = plan([])
    season_plan.no_exists["tmdb:1"][1].total_episode = 1200
    assert len(search_targets(season_plan)) == 1200
    assert SearchScan(owner=owner, plan=season_plan).full
    season_plan.no_exists["tmdb:1"][1].total_episode = 9999
    assert search_targets(season_plan) == {"1:season"}
    assert SearchScan(owner=owner, plan=season_plan).full


def test_whole_season_waits_for_late_pack_across_sites_and_checkpoint(owner, monkeypatch):
    """
    散集关闭、站点冷却和检查点恢复都不能先于后页整季包提交。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.chain.search.scan import ScanPage

    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [
        {"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    season_plan = plan([])
    season_plan.no_exists["tmdb:1"][1].total_episode = 3
    pack = TorrentInfo(site=2, description="complete", title="Show 2026 S01 Complete 1080p",
                       enclosure="https://site.example/download?id=complete")
    data = {"1:Show": {0: [torrent(1)], 1: [torrent(2)]},
            "2:Show": {0: [torrent(1)], 1: [torrent(2)], 2: [pack]}}
    calls = []
    cooling = {"active": True}

    def fetch(_sources, key, page, **_params):
        """
        第二站点下一页暂时冷却，其他来源可继续推进到末页。
        """
        if key == "2:Show" and page == 1 and cooling["active"]:
            retry = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
            return ScanPage([], SiteSearchObservation(), retry, "cooldown")
        calls.append((key, page))
        items = data.get(key, {}).get(page, [])
        return ScanPage(items, SiteSearchObservation(True, "success", raw_count=len(items)))

    monkeypatch.setattr(SearchSources, "fetch", fetch)
    initial = SearchScan(owner=owner, plan=season_plan)
    submitted = []

    def submit(items, ready):
        """
        所有来源搜完才统一交给既有下载择优入口。
        """
        assert all(source.exhausted for source in scan.collection.sources.values())
        submitted.append(({item.torrent_info.description for item in items}, ready))
        return ready

    scan = initial
    scan.run(submit)
    assert submitted == []
    assert scan.collection.sources["1:Show"].exhausted
    assert scan.collection.sources["2:Show"].next_page == 1
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(scan.state()))
    scan = SearchScan(owner=owner, plan=season_plan, snapshot=snapshot)
    assert not scan.restart
    cooling["active"] = False
    scan.retry.clear()
    scan.run(submit)
    assert len(submitted) == 1
    assert submitted[0][0] == {"1-1", "2-1", "complete"}
    assert submitted[0][1] == {"1:1", "1:2", "1:3"}
    assert calls.count(("2:Show", 0)) == 1
    assert not scan.collection.remaining


def test_whole_season_without_complete_pack_still_submits_parts_at_the_end(owner, monkeypatch):
    """
    搜完没有整季包时照常下载散集，保留逐集结算而不丢弃候选。
    """
    season_plan = plan([])
    season_plan.no_exists["tmdb:1"][1].total_episode = 2
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(2)]})
    submitted = []
    scan = SearchScan(owner=owner, plan=season_plan)
    scan.run(lambda items, ready: submitted.append((calls[-1], len(items), ready)) or ready)
    assert submitted == [(2, 2, {"1:1", "1:2"})]
    assert not scan.collection.remaining


def test_full_mode_checks_aliases_when_first_name_only_has_older_episodes(owner, monkeypatch):
    """
    前一名称只找到旧集时，完整模式仍须翻完并尝试后续名称。
    """
    monkeypatch.setattr(owner, "_prepare_params", lambda **_params: (None, ["First", "Alias"]))
    calls = []

    def request(**params):
        """
        第一个名称没有目标集，第二个名称才找到它。
        """
        calls.append((params["keyword"], params["page"]))
        items = [] if params["page"] else [torrent(1 if params["keyword"] == "First" else 3)]
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items

    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([3]), full=True)
    scan.run(lambda _items, ready: ready)
    assert calls == [("First", 0), ("First", 1), ("Alias", 0), ("Alias", 1)]
    assert not scan.collection.remaining


def test_whole_season_restarts_old_smart_checkpoint_and_keeps_submitted_targets(owner, monkeypatch):
    """
    旧智能模式检查点需重搜以补齐整季候选，已经提交的集数仍保留。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    old = SearchScan(owner=owner, plan=plan([1, 2]))
    old.collection.settle({"1:1"})
    old.submitted.add("1:1")
    season_plan = plan([])
    season_plan.no_exists["tmdb:1"][1].total_episode = 2
    scan = SearchScan(owner=owner, plan=season_plan,
                      snapshot=SearchSessionSnapshot("round", 0, encode_search_state(old.state())))
    assert scan.restart and scan.full
    assert scan.collection.settled == {"1:1"}
    calls = pages(owner, monkeypatch, {0: [torrent(1), torrent(2)]})
    batches = []
    scan.run(lambda _items, ready: batches.append(ready) or ready)
    assert calls == [0, 1]
    assert batches == [{"1:2"}]
    assert scan.submitted == {"1:1", "1:2"}


def test_no_candidate_filter_drives_full_fallback_from_current_cursor(owner, monkeypatch):
    """
    可用候选被过滤后，从当前游标继续完整搜索回退资源。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(4)], 2: [torrent(1, 2)]})
    submitted = []
    def submit(contexts, ready):
        submitted.extend(contexts)
        return ready if any(item.torrent_info.description == "1-2" for item in contexts) else set()
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(submit)
    assert calls == [0, 1, 2, 3]
    assert [item.torrent_info.description for item in submitted] == ["1-1", "1-2"]


def test_unchanged_resource_recognized_once_even_when_repeated(owner, monkeypatch):
    """
    同一未变化资源在多页重复出现时只识别一次。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(1), torrent(4)]})
    from app.chain.search import result as result_module
    original = result_module._torrent_meta
    identified = []
    def identify(**params):
        identified.append(params["torrent"].description)
        return original(**params)
    monkeypatch.setattr(result_module, "_torrent_meta", identify)
    SearchScan(owner=owner, plan=plan([1, 4])).run(lambda _items, ready: ready)
    assert calls == [0, 1, 2]
    assert identified == ["1-1", "4-1"]


def test_twelve_hundred_episodes_with_multiple_versions(owner, monkeypatch):
    """
    20 页范围内仍能收集 1200 集的多版本候选，不在最后一页遗漏提交。
    """
    data = {page: [torrent(episode, version) for episode in range(page * 60 + 1, (page + 1) * 60 + 1)
                   for version in (1, 2)] for page in range(20)}
    calls = pages(owner, monkeypatch, data)
    scan = SearchScan(owner=owner, plan=plan(list(range(1, 1201))))
    scan.run(lambda _items, ready: ready)
    assert calls == list(range(20))
    assert not scan.collection.remaining
    assert len(scan.identity) == 2400


def test_twenty_page_cap_ends_the_round_and_does_not_repeat(owner, monkeypatch):
    """
    达到 20 页后结束本轮，仍缺集也不会重新扫描已达上限的来源。
    """
    # 页面出现更新的集，缺失的 1200 属于站点已收录范围，需要一直翻到上限。
    calls = pages(owner, monkeypatch, {page: [torrent(1300, page + 1)] for page in range(125)})
    scan = SearchScan(owner=owner, plan=plan([1200]))
    scan.run(lambda _items, ready: ready)
    assert calls == list(range(20))
    assert scan.collection.remaining == {"1:1200"}
    scan.run(lambda _items, ready: ready)
    assert len(calls) == 20


def test_full_mode_collects_late_candidates_after_a_gap(owner, monkeypatch):
    """
    完整搜索跨过无目标候选的间隔页，仍收集更后面的资源。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(4)], 2: [torrent(1, 2)]})
    chosen = []
    scan = SearchScan(owner=owner, plan=plan([1]), full=True)
    scan.run(lambda items, ready: chosen.extend(item.torrent_info.description for item in items) or ready)
    assert calls == [0, 1, 2, 3]
    assert set(chosen) == {"1-1", "1-2"}


def test_pack_waits_for_all_covered_goals_and_is_submitted_once(owner, monkeypatch):
    """
    合集等待其覆盖目标全部具备选择条件后只提交一次。
    """
    pack = TorrentInfo(site=1, description="pack", title="Show S01E01-E04 2026 1080p", enclosure="https://site.example/download?id=pack")
    calls = pages(owner, monkeypatch, {0: [pack], 1: [torrent(4)], 2: [torrent(9)]})
    batches = []
    scan = SearchScan(owner=owner, plan=plan([1, 4]))
    scan.run(lambda items, ready: batches.append((calls[-1], {item.torrent_info.description for item in items}, ready)) or ready)
    assert len(batches) == 1
    assert batches[0][0] == 2
    assert "pack" in batches[0][1]
    assert batches[0][2] == {"1:1", "1:4"}


def test_single_episode_can_download_while_a_pack_waits(owner, monkeypatch):
    """
    合集仍在等待时，独立集数可以先提交下载。
    """
    pack = TorrentInfo(site=1, description="pack", title="Show S01E01-E04 2026 1080p", enclosure="https://site.example/download?id=pack")
    calls = pages(owner, monkeypatch, {0: [pack, torrent(1)], 1: [torrent(4)], 2: [torrent(9)]})
    batches = []
    scan = SearchScan(owner=owner, plan=plan([1, 4]))
    scan.run(lambda items, ready: batches.append((calls[-1], {item.torrent_info.description for item in items}, ready)) or ready)
    assert batches[0] == (1, {"1-1"}, {"1:1"})
    assert sum("pack" in items for _, items, _ in batches) == 1


def test_cross_site_barrier_keeps_late_opportunistic_candidate(owner, monkeypatch):
    """
    跨站点等待期间保留其他站点稍后发现的可用候选。
    """
    monkeypatch.setattr(owner, "_sync_indexers", lambda _ids: [{"id": 1}, {"id": 2}])
    data = {1: {0: [torrent(1)], 1: [torrent(4)], 2: [torrent(1, 7), torrent(4, 2)]},
            2: {0: [torrent(9)], 1: [torrent(1, 3), torrent(4, 3)], 2: [torrent(9, 2)]}}
    calls = []
    def request(**params):
        site, page = params["site"]["id"], params["page"]
        calls.append((site, page))
        items = data[site].get(page, [])
        for item in items:
            item.site = site
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items
    monkeypatch.setattr(owner, "search_site_torrents", request)
    batches = []
    SearchScan(owner=owner, plan=plan([1, 4])).run(
        lambda items, ready: batches.append((calls[-1], {item.torrent_info.description for item in items}, ready)) or ready)
    assert batches[0][0] == (2, 2)
    assert "1-7" in batches[0][1]
    assert batches[0][2] == {"1:1"}


def test_rejected_candidate_is_not_retried_in_the_same_run_but_rejudged_after_resume(owner, monkeypatch):
    """
    同轮已拒绝候选不重复尝试，恢复搜索后允许重新判断。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    first = torrent(1)
    first.downloadvolumefactor = 1
    free = torrent(1)
    free.downloadvolumefactor = 0
    pages(owner, monkeypatch, {0: [first], 1: [torrent(4)], 2: [free]})
    attempts = []
    def submit(items, ready):
        attempts.extend(item.torrent_info.downloadvolumefactor for item in items)
        return ready if any(item.torrent_info.downloadvolumefactor == 0 for item in items) else set()
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(submit)
    # 本轮内已处理过的同一资源不再重复提交。
    assert attempts == [1]
    assert scan.collection.remaining == {"1:1"}
    # 已处理标记不入检查点：恢复后按最新信息（已免费）重新判断。
    resumed = SearchScan(owner=owner, plan=plan([1]),
                         snapshot=SearchSessionSnapshot("task", 1, encode_search_state(scan.state())))
    resumed.ended = False
    resumed.choose({"1:1"}, submit)
    assert attempts == [1, 0]
    assert not resumed.collection.remaining


def test_site_window_wait_keeps_cursor(owner, monkeypatch):
    """
    等待站点请求窗口时保留页游标，并报告冷却原因。
    """
    def request(**_params):
        report_site_search_outcome(attempted=False, outcome="deferred", error="站点窗口限流")
        report_site_search_outcome(attempted=False, outcome="skipped")
        return []
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1]))
    key = next(key for key in scan.queries if not key.startswith("plugin:"))
    assert not scan.step(key)
    assert scan.retry_reasons[key] == "cooldown"
    assert scan.collection.sources[key].next_page == 0
    assert not scan.collection.sources[key].failed
    assert not scan.collection.sources[key].exhausted
    assert scan.collection.sources[key].error == "站点窗口限流"


def test_site_rate_limit_window_defers_retry_until_window_refresh(owner, monkeypatch):
    """
    站点触发计数窗口限流时按窗口长度退避，不能继续用秒级冷却反复重试。
    """
    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [
        {"id": 1, "name": "test", "parser": "mTorrent",
         "limit_interval": 3600, "limit_count": 25, "limit_seconds": 20}
    ])
    def request(**_params):
        report_site_search_outcome(
            attempted=False, outcome="deferred",
            error="触发流控规则，3600 秒内访问次数不得超过 25 次",
        )
        return []
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1]))
    key = next(key for key in scan.queries if not key.startswith("plugin:"))
    before = datetime.now(timezone.utc)

    assert not scan.step(key)

    assert datetime.fromisoformat(scan.retry[key]) >= before + timedelta(seconds=3600)
    assert scan.retry_reasons[key] == "cooldown"
    assert not scan.collection.sources[key].failed
    assert not scan.collection.sources[key].exhausted


def test_scan_persists_local_rate_limit_window_on_site_budget(owner, monkeypatch):
    """
    本地流控退避必须写入站点预算，让其它订阅在窗口内共享同一冷却。
    """
    from app.application.subscription.sitebudget import SiteBudgetClaim, SubscriptionSiteBudget
    from app.runtime.stop import ProcessStopState

    captured = {}

    class _Repository:
        """
        发放独占租约并记录收口参数。
        """

        def claim_site(self, *, site_id, owner, lease_seconds, minimum_interval=0):
            """
            返回已取得的固定站点租约，隔离真实站点预算存储。
            """
            del owner, lease_seconds, minimum_interval
            return SiteBudgetClaim(
                site_id=site_id,
                acquired=True,
                retry_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                consecutive_failures=0,
                lease_token="lease-token",
            )

        def finish_site(self, **kwargs):
            """
            记录租约结束参数并返回成功，供失败原因与退避断言使用。
            """
            captured.update(kwargs)
            return True

    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [
        {"id": 1, "name": "test", "parser": "mTorrent",
         "limit_interval": 3600, "limit_count": 25, "limit_seconds": 20}
    ])
    def request(**_params):
        report_site_search_outcome(attempted=False, outcome="deferred", error="触发流控规则")
        return []
    monkeypatch.setattr(owner, "search_site_torrents", request)
    owner.configure_subscription_site_budget(SubscriptionSiteBudget(
        repository=_Repository(), owner="scan-worker", cancelled=lambda: False,
        stop_state=ProcessStopState(),
    ))
    scan = SearchScan(owner=owner, plan=plan([1]))
    key = next(key for key in scan.queries if not key.startswith("plugin:"))
    before = datetime.now(timezone.utc)

    assert not scan.step(key)

    assert captured["outcome"] == "deferred"
    assert captured["error"] == "触发流控规则"
    assert datetime.fromisoformat(captured["next_allowed_at"]) >= before + timedelta(seconds=3600)


def test_defer_ignores_expired_retries_and_terminal_sources(owner, monkeypatch):
    """
    失败或耗尽来源的检查点残留不能覆盖仍活跃来源的冷却时间。
    """
    from app.application.subscription.sitebudget import SubscriptionSearchDeferred

    monkeypatch.setattr(owner, "_sync_indexers", lambda _sites: [
        {"id": site_id, "name": f"test-{site_id}", "parser": "mTorrent"} for site_id in (1, 2, 3)
    ])
    scan = SearchScan(owner=owner, plan=plan([1]))
    failed_key, exhausted_key, active_key = (f"{site_id}:Show" for site_id in (1, 2, 3))
    scan.collection.sources[failed_key].failed = True
    scan.collection.sources[exhausted_key].exhausted = True
    now = datetime.now(timezone.utc)
    expired = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    active_retry = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
    scan.retry = {failed_key: expired, exhausted_key: expired, active_key: active_retry}
    scan.retry_reasons = {failed_key: "cooldown", exhausted_key: "cooldown", active_key: "busy"}

    with pytest.raises(SubscriptionSearchDeferred) as error:
        scan._defer()

    assert error.value.retry_at == active_retry
    assert error.value.wait_reason == "busy"
    assert scan.retry == {active_key: active_retry}
    assert scan.retry_reasons == {active_key: "busy"}


def test_defer_uses_request_interval_when_all_retries_expired(owner):
    """
    所有冷却都已过期时至少等待一个请求间隔再交还订阅队列。
    """
    from app.application.subscription.sitebudget import SubscriptionSearchDeferred

    scan = SearchScan(owner=owner, plan=plan([1]))
    key = next(key for key in scan.collection.active_sources() if not key.startswith("plugin:"))
    scan.retry[key] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    scan.retry_reasons[key] = "cooldown"
    before = datetime.now(timezone.utc)

    with pytest.raises(SubscriptionSearchDeferred) as error:
        scan._defer()

    retry_at = datetime.fromisoformat(error.value.retry_at)
    assert retry_at >= before + timedelta(seconds=AUTOMATIC_REQUEST_INTERVAL)
    assert error.value.wait_reason == "slice"
    assert not scan.retry
    assert not scan.retry_reasons


def test_all_sources_failing_is_not_reported_as_exhausted(owner, monkeypatch):
    """
    所有来源请求失败时不得误报为资源已搜尽。
    """
    from app.application.subscription.execution import SubscriptionSiteSearchFailed
    def request(**_params):
        report_site_search_outcome(attempted=True, outcome="error", error="timeout")
        return []
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1]))
    with pytest.raises(SubscriptionSiteSearchFailed, match="timeout"):
        scan.run(lambda *_args: pytest.fail("失败来源不能提交"))
    source = next(value for key, value in scan.collection.sources.items() if not key.startswith("plugin:"))
    assert not source.exhausted
    assert source.next_page == 0
    assert "部分来源失败" in scan.progress_text()


def test_language_or_capability_skip_keeps_existing_non_failure_semantics(owner, monkeypatch):
    """
    语言或站点能力不支持导致跳过时，保持非失败语义。
    """
    monkeypatch.setattr(owner, "search_site_torrents", lambda **_params: [])
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda *_args: pytest.fail("未发出请求不得提交"))
    assert scan.ended
    assert scan.collection.remaining == {"1:1"}


def test_opaque_ticket_recovery_submits_first_page_before_waiting_and_does_not_repeat_it_after_restart(
    owner, monkeypatch, scan_clock,
):
    """
    不透明下载凭据恢复时先提交首页可用目标，重启后不重复提交。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.application.subscription.sitebudget import SubscriptionSearchDeferred
    from app.chain.search.scan import ScanPage
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda items: sorted(items, key=lambda item: item.torrent_info.description)))
    data = {0: [torrent(1)], 1: [torrent(2)]}
    for items in data.values():
        items[0].enclosure = "https://site.example/download?ticket=do-not-store"
    pages(owner, monkeypatch, data)
    initial = SearchScan(owner=owner, plan=plan([1, 2]), full=True)
    source = next(key for key in initial.queries if not key.startswith("plugin:"))
    for page in range(3):
        assert initial.step(source)
    assert initial.step("plugin:Show")
    def restore(_method, **params):
        """
        恢复候选事实，缺失票据交给原页重取。
        """
        item = TorrentInfo(**params["record"])
        return item if item.enclosure or params.get("allow_missing") else None
    monkeypatch.setattr(owner, "run_module", restore)
    calls = []
    cooling = {"active": True}
    def fetch(_sources, _key, page, **_params):
        """
        第二页等待一次冷却，其余页面直接返回票据。
        """
        if page == 1 and cooling["active"]:
            retry = (scan_clock["now"] + timedelta(seconds=10)).isoformat()
            return ScanPage([], SiteSearchObservation(), retry, "cooldown")
        calls.append(page)
        return ScanPage(data[page], SiteSearchObservation(True, "success", raw_count=1))
    monkeypatch.setattr(SearchSources, "fetch", fetch)
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(initial.state()))
    restored = SearchScan(owner=owner, plan=plan([1, 2]), full=True, snapshot=snapshot)
    batches = []
    with pytest.raises(SubscriptionSearchDeferred):
        restored.run(lambda _items, goals: batches.append(goals) or goals)
    assert batches == [{"1:1"}]
    assert calls == [0]
    payload = encode_search_state(restored.state())
    assert "do-not-store" not in payload
    cooling["active"] = False
    scan_clock["now"] = datetime.fromisoformat(restored.retry[source]) + timedelta(seconds=1)
    resumed = SearchScan(owner=owner, plan=plan([2]), full=True,
                         snapshot=SearchSessionSnapshot("round", 1, payload))
    resumed.run(lambda _items, goals: batches.append(goals) or goals)
    assert batches == [{"1:1"}, {"1:2"}]
    assert calls == [0, 1]


@pytest.mark.parametrize("part_needs_recovery", [False, True])
def test_whole_season_recovers_late_complete_pack_before_submitting_parts(owner, monkeypatch, part_needs_recovery):
    """
    完整搜索中整季包的临时地址恢复必须先于前页分集提交。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.chain.search.scan import ScanPage

    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(
        lambda items: sorted(items, key=lambda item: item.torrent_info.description)))
    parts = [torrent(1), torrent(2)]
    if part_needs_recovery:
        parts[1].enclosure = "https://site.example/download?ticket=part-do-not-store"
    pack = TorrentInfo(
        site=1,
        description="pack",
        title="Show S01 Complete 2026 1080p",
        enclosure="https://site.example/download?ticket=do-not-store",
    )
    pages(owner, monkeypatch, {0: parts, 1: [pack]})
    initial = SearchScan(owner=owner, plan=plan([1, 2]), full=True, prefer_complete=True)
    source = next(key for key in initial.queries if not key.startswith("plugin:"))
    for page in range(3):
        assert initial.step(source)
    assert initial.step("plugin:Show")

    def restore(_method, **params):
        """
        从检查点恢复公开下载地址，临时票据交给原页重新获取。
        """
        return TorrentInfo(**params["record"])

    monkeypatch.setattr(owner, "run_module", restore)
    recovered = TorrentInfo(
        site=1,
        description="pack",
        title="Show S01 Complete 2026 1080p",
        enclosure="https://site.example/download?id=pack",
    )
    fetches = []

    def fetch(_sources, key, page, **_params):
        """
        只为整季候选重新请求原页并返回可下载地址。
        """
        fetches.append((key, page))
        return ScanPage([recovered], SiteSearchObservation(True, "success", raw_count=1))

    monkeypatch.setattr(SearchSources, "fetch", fetch)
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(initial.state()))
    scan = SearchScan(owner=owner, plan=plan([1, 2]), full=True, snapshot=snapshot, prefer_complete=True)
    submitted = []
    scan.choose({"1:1", "1:2"}, lambda items, goals: submitted.append(
        ({item.torrent_info.description for item in items}, goals)) or goals)

    assert fetches == [(source, 1)]
    expected = {"1-1", "pack"} if part_needs_recovery else {"1-1", "2-1", "pack"}
    assert submitted == [(expected, {"1:1", "1:2"})]


def test_one_unidentifiable_resource_does_not_block_the_page(owner, monkeypatch):
    """
    一条无法识别的资源不阻断同页其他候选处理。
    """
    calls = pages(owner, monkeypatch, {0: [TorrentInfo(site=1, description="noise", title=""), torrent(1)]})
    submitted = []
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda items, ready: submitted.extend(item.torrent_info.description for item in items) or ready)
    assert submitted == ["1-1"]
    assert calls == [0, 1]
    assert not scan.collection.remaining


def test_restored_scope_recomputes_targets_for_resources_without_season(owner, monkeypatch):
    """
    恢复后的目标范围变化时，重新计算缺少季号资源的覆盖目标。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    pages(owner, monkeypatch, {0: [TorrentInfo(site=1, description="e1-2", title="Show E01-E02 2026 1080p",
                                                enclosure="https://site.example/download?id=2")]})
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.step(next(key for key in scan.queries if not key.startswith("plugin:")))
    key = next(iter(scan.candidates))
    assert scan.identity[key]["targets"] == ["1:1"]
    snapshot = SearchSessionSnapshot("scope", 1, encode_search_state(scan.state()))
    restored = SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)
    assert restored.identity[key]["targets"] == ["1:1", "1:2"]


def test_checkpoint_keeps_only_candidates_covering_remaining_episodes(owner, monkeypatch):
    # 连载动画大量旧集资源与本轮缺集无关，只留在本进程识别缓存，不写入检查点。
    """
    检查点只保留仍覆盖缺集的候选，避免无关历史资源膨胀。
    """
    pages(owner, monkeypatch, {0: [torrent(ep) for ep in range(1, 31)]})
    scan = SearchScan(owner=owner, plan=plan([20]))
    scan.step(next(key for key in scan.queries if not key.startswith("plugin:")))
    assert len(scan.identity) == 30
    state = scan.state()
    assert len(state["candidates"]) == 1
    assert set(state["identity"]) == set(state["candidates"])
    assert state["identity"][next(iter(state["candidates"]))]["targets"] == ["1:20"]


def test_progress_log_summary_reports_page_and_remaining_episodes(owner, monkeypatch):
    """
    搜索进度摘要包含当前页、已提交目标和剩余集数。
    """
    pages(owner, monkeypatch, {0: [torrent(ep) for ep in range(16, 38)]})
    scan = SearchScan(owner=owner, plan=plan(list(range(1, 38))))
    for key in list(scan.collection.sources):
        scan.step(key)
    assert scan.progress_text() == "已搜索到第 1 页，已提交：无，仍缺：E01-E37"

def test_site_repeating_the_same_page_ends_the_source_instead_of_looping(owner, monkeypatch):
    """
    站点反复返回同一页时结束该来源，防止无限翻页。
    """
    repeated = [torrent(9)]
    calls = []
    def request(**params):
        calls.append(params["page"])
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1)
        return [torrent(1)] if params["page"] == 0 else repeated
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1, 2]))
    scan.run(lambda _items, ready: ready)
    assert calls == [0, 1, 2]
    assert scan.ended
    assert scan.collection.remaining == {"1:2"}


def _snapshot_after_first_pages(owner, episodes, **changes):
    import json

    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    scan = SearchScan(owner=owner, plan=plan(episodes))
    source = next(key for key in scan.queries if not key.startswith("plugin:"))
    scan.step(source)
    scan.step(source)
    state = json.loads(encode_search_state(scan.state()))
    state.update(changes.pop("state", {}))
    for name, value in changes.items():
        state["collection"]["sources"][source][name] = value
    return SearchSessionSnapshot("task", 1, encode_search_state(state)), source


def test_resume_after_fifteen_minutes_loads_next_page_then_refreshes_first_page(owner, monkeypatch):
    """
    间隔十五分钟恢复搜索时先请求下一页，再刷新首页。
    """
    pages(owner, monkeypatch, {0: [torrent(5)], 1: [torrent(6)], 2: [torrent(1)]})
    snapshot, source = _snapshot_after_first_pages(owner, [1, 2], last_request_at=0)
    # 中断期间第一页出现了新资源。
    calls = pages(owner, monkeypatch, {0: [torrent(2), torrent(5)], 1: [torrent(6)], 2: [torrent(1)]})
    resumed = SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)
    resumed.step(source)
    resumed.step(source)
    assert calls == [2, 0]
    assert resumed.collection.sources[source].next_page == 3
    assert {item["torrent"]["description"] for item in resumed.candidates.values()} >= {"2-1", "1-1"}


def test_failed_first_page_refresh_keeps_history_searchable(owner, monkeypatch):
    """
    首页刷新失败不封闭历史页搜索，也不保留错误的刷新状态。
    """
    pages(owner, monkeypatch, {0: [torrent(5)], 1: [torrent(6)], 2: [torrent(1)]})
    snapshot, source = _snapshot_after_first_pages(owner, [1, 2], last_request_at=0)
    resumed = SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)
    resumed.step(source)
    def failing(**_params):
        report_site_search_outcome(attempted=True, outcome="error", error="timeout")
        return []
    monkeypatch.setattr(owner, "search_site_torrents", failing)
    resumed.step(source)
    cursor = resumed.collection.sources[source]
    assert not (cursor.head_refresh or cursor.refresh_pending)
    # 补拉失败不关闭来源，下一次回到原历史页继续。
    assert not cursor.failed
    assert source in resumed.collection.active_sources()
    calls = pages(owner, monkeypatch, {3: [torrent(2)]})
    resumed.step(source)
    assert calls == [3]
    assert cursor.next_page == 4


def test_checkpoint_older_than_thirty_six_hours_restarts_from_first_page(owner, monkeypatch):
    """
    超过三十六小时的检查点从首页重建搜索。
    """
    import time

    pages(owner, monkeypatch, {0: [torrent(5)], 1: [torrent(6)]})
    snapshot, source = _snapshot_after_first_pages(owner, [1, 2], state={"saved_at": time.time() - 37 * 3600})
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(2)]})
    resumed = SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)
    resumed.collection.settled = {"1:2"}
    resumed.submitted = {"1:2"}
    submitted = []
    resumed.run(lambda items, ready: submitted.extend(item.torrent_info.description for item in items) or ready)
    assert calls[0] == 0
    # 重搜保留已提交的集，不再重复下载。
    assert submitted == ["1-1"]
    assert "1:2" in resumed.collection.settled and "1:2" in resumed.submitted
    assert not resumed.collection.remaining


def test_failed_episode_falls_back_and_uses_a_later_page_candidate_after_sources_finish(owner, monkeypatch):
    # 第 0 页有 E1，第 1 页没有：E1 结束收集并提交，但下载失败；
    # 为 E5 继续翻页时第 2 页又出现新的 E1，兜底在来源翻完后用它重新择优，失败的不再重试。
    """
    单集下载失败后，在来源结束时使用后续页候选回退。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(1, 1), torrent(5)], 1: [torrent(5, 2)],
                                       2: [torrent(1, 2), torrent(5, 3)]})
    attempts = []
    def submit(items, ready):
        attempts.append(sorted(item.torrent_info.description for item in items))
        return {goal for goal in ready if goal != "1:1"} | ({"1:1"} if any(
            item.torrent_info.description == "1-2" for item in items) else set())
    scan = SearchScan(owner=owner, plan=plan([1, 5]))
    scan.run(submit)
    assert ["1-1"] in attempts
    assert "1-2" in attempts[-1] and "1-1" not in attempts[-1]
    assert calls == [0, 1, 2, 3]
    assert not scan.collection.remaining


@pytest.mark.parametrize("field,value", [("media_id", "2"), ("media_source", MediaSource.Douban),
                                        ("episode_group", "alternate")])
def test_changed_media_checkpoint_drops_old_settled_episodes(owner, monkeypatch, field, value):
    """
    新作品即使有相同季集号，也不能继承旧作品的提交事实。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    calls = pages(owner, monkeypatch, {0: [torrent(1), torrent(2)]})
    scan = SearchScan(owner=owner, plan=plan([1, 2]))
    scan.collection.settle({"1:1"})
    scan.submitted.add("1:1")
    snapshot = SearchSessionSnapshot("task", 1, encode_search_state(scan.state()))
    changed = plan([1, 2])
    setattr(changed.mediainfo, field, value)
    resumed = SearchScan(owner=owner, plan=changed, snapshot=snapshot)
    submitted = []
    resumed.run(lambda items, ready: submitted.extend(item.torrent_info.description for item in items) or ready)
    assert calls[0] == 0
    assert set(submitted) == {"1-1", "2-1"}
    assert not resumed.collection.remaining


def test_same_media_changed_keyword_keeps_settled_episodes(owner, monkeypatch):
    """
    同作品改关键词可从首页重搜，但已提交的集仍不重复下载。
    """
    from dataclasses import replace

    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    pages(owner, monkeypatch, {0: [torrent(1), torrent(2)]})
    scan = SearchScan(owner=owner, plan=plan([1, 2]))
    scan.collection.settle({"1:1"})
    scan.submitted.add("1:1")
    snapshot = SearchSessionSnapshot("task", 1, encode_search_state(scan.state()))
    resumed = SearchScan(owner=owner, plan=replace(plan([1, 2]), keyword="Alias"), snapshot=snapshot)
    submitted = []
    resumed.run(lambda items, ready: submitted.extend(item.torrent_info.description for item in items) or ready)
    assert submitted == ["2-1"]
    assert resumed.submitted == {"1:1", "1:2"}


def test_checkpoint_requires_media_identity(owner):
    """
    检查点必须携带媒体身份，不再用搜索合同兼容缺少该字段的旧格式。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    scan = SearchScan(owner=owner, plan=plan([1, 2]))
    state = scan.state()
    del state["media_identity"]
    snapshot = SearchSessionSnapshot("task", 1, encode_search_state(state))
    with pytest.raises(KeyError, match="media_identity"):
        SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)


def test_finished_round_deletes_checkpoint(owner, monkeypatch, tmp_path):
    """
    搜索轮次完成后删除对应检查点。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.application.subscription.execution import SubscriptionExecutionAdmission, SubscriptionExecutionContext
    from app.db.adapters.searchsession import TransactionalSearchSessionRepository
    from app.db.base import Base
    from app.db.models.subscriptionsearch import SubscriptionSearchTask

    engine = create_engine(f"sqlite:///{tmp_path / 'finish.db'}")
    Base.metadata.create_all(engine)
    repo = TransactionalSearchSessionRepository(sessionmaker(bind=engine))
    now = datetime.now(timezone.utc).isoformat()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    with sessionmaker(bind=engine)() as db:
        db.add(SubscriptionSearchTask(task_id="task", batch_id="batch", subscription_id=1, source="manual",
               position=0, state="running", created_at=now, updated_at=now, lease_token="lease", lease_expires_at=expiry))
        db.commit()
    admission = SubscriptionExecutionAdmission()
    lease = admission.try_acquire(subscription_id=1, operation="search", ttl_seconds=300)
    execution = SubscriptionExecutionContext(lease, admission, task_id="task", task_lease="lease")
    pages(owner, monkeypatch, {0: [torrent(1)]})
    scan = SearchScan(owner=owner, plan=plan([1]), execution=execution, repository=repo)
    scan.run(lambda _items, ready: ready)
    assert scan.ended
    assert repo.get(task_id="task") is None
    engine.dispose()


def test_ongoing_series_releases_round_after_available_episodes_are_submitted(owner, monkeypatch):
    # 连载到 1005 集：补完可下载的 1003 后，1006/1007 站点尚未收录，不深翻历史页，本轮结束交给订阅模式追更。
    """
    连载剧已发现的集数提交后结束本轮，不等待尚未播出的目标。
    """
    calls = pages(owner, monkeypatch, {0: [torrent(ep) for ep in range(1005, 995, -1)],
                                     1: [torrent(ep) for ep in range(995, 985, -1)],
                                     2: [torrent(ep) for ep in range(985, 975, -1)]})
    submitted = []
    scan = SearchScan(owner=owner, plan=plan([1003, 1006, 1007]))
    scan.run(lambda _items, ready: submitted.append(set(ready)) or ready)
    assert calls == [0, 1]
    assert submitted == [{"1:1003"}]
    assert scan.ended
    assert scan.collection.remaining == {"1:1006", "1:1007"}
    assert "E1006-E1007 站点尚未收录" in scan.progress_text()


def test_only_unreleased_episodes_check_first_page_and_do_not_try_aliases(owner, monkeypatch):
    """
    只缺未播出集数时检查首个关键词首页，不继续尝试别名。
    """
    monkeypatch.setattr(owner, "_prepare_params", lambda **_params: (None, ["First", "Alias"]))
    calls = []
    def request(**params):
        calls.append((params["keyword"], params["page"]))
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=1, has_more=True)
        return [torrent(1005 - params["page"])]
    monkeypatch.setattr(owner, "search_site_torrents", request)
    scan = SearchScan(owner=owner, plan=plan([1006]))
    scan.run(lambda _items, ready: ready)
    assert calls == [("First", 0)]
    assert scan.ended


def test_site_without_paging_is_checked_once_without_reporting_failure(owner, monkeypatch):
    """
    不支持分页的站点只检查一次，并保持非失败状态。
    """
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: None)
    calls = pages(owner, monkeypatch, {0: [torrent(2)], 1: [torrent(1)]})
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda _items, ready: ready)
    assert calls == [0]
    assert not any(source.failed for source in scan.collection.sources.values())
    assert "部分来源失败" not in scan.progress_text()


def test_unchanged_checkpoint_is_not_written_again(owner, monkeypatch):
    """
    检查点内容未变化时不重复持久化。
    """
    from types import SimpleNamespace

    from app.application.search.session import SearchSessionSnapshot

    class Repository:
        """
        保存搜索扫描检查点的内存仓库，验证创建与更新调用。
        """
        def __init__(self):
            self.writes = 0

        def create(self, **_params):
            """
            记录检查点创建次数，并返回版本为零的固定快照。
            """
            self.writes += 1
            return SearchSessionSnapshot("task", 0, "{}")

        def save(self, *, snapshot, **_params):
            """
            记录检查点保存次数，返回版本递增后的快照。
            """
            self.writes += 1
            return SearchSessionSnapshot("task", snapshot.version + 1, "{}")

    pages(owner, monkeypatch, {0: [torrent(1)]})
    repository = Repository()
    execution = SimpleNamespace(task_id="task", task_lease="lease", is_cancel_requested=lambda: False)
    scan = SearchScan(owner=owner, plan=plan([1, 2]), execution=execution, repository=repository)
    scan.checkpoint()
    scan.checkpoint()
    assert repository.writes == 1
    scan.step(next(key for key in scan.queries if not key.startswith("plugin:")))
    assert repository.writes == 2


def test_candidate_whose_page_keeps_failing_is_dropped_after_limited_retries(owner, monkeypatch):
    # 最优候选的临时票据所在页一直重取失败：有限次重试后放弃，次优候选继续提交，不卡住整个订阅。
    """
    候选所在页持续失败时仅有限重试，之后丢弃该候选并继续回退。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.application.subscription.sitebudget import SubscriptionSearchDeferred
    from app.chain.search.scan import RECOVERY_ATTEMPTS, ScanPage

    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda items: sorted(items, key=lambda item: item.torrent_info.description)))
    best, other = torrent(1, 1), torrent(1, 2)
    best.enclosure = "https://site.example/download?ticket=do-not-store"
    pages(owner, monkeypatch, {0: [best], 1: [other]})
    initial = SearchScan(owner=owner, plan=plan([1]), full=True)
    source = next(key for key in initial.queries if not key.startswith("plugin:"))
    for _page in range(3):
        initial.step(source)
    initial.step("plugin:Show")
    monkeypatch.setattr(owner, "run_module", lambda _method, **params: TorrentInfo(**params["record"]))
    requests = []
    def fetch(_sources, _key, page, **_params):
        requests.append(page)
        return ScanPage([], SiteSearchObservation(True, "error", error="站点超时"))
    monkeypatch.setattr(SearchSources, "fetch", fetch)
    scan = SearchScan(owner=owner, plan=plan([1]), full=True,
                      snapshot=SearchSessionSnapshot("round", 0, encode_search_state(initial.state())))
    submitted = []
    for _attempt in range(RECOVERY_ATTEMPTS - 1):
        with pytest.raises(SubscriptionSearchDeferred):
            scan.run(lambda items, goals: submitted.extend(i.torrent_info.description for i in items) or goals)
    # 失败次数随检查点保存，重启后不会重新计数。
    scan = SearchScan(owner=owner, plan=plan([1]), full=True,
                      snapshot=SearchSessionSnapshot("round", 1, encode_search_state(scan.state())))
    scan.run(lambda items, goals: submitted.extend(i.torrent_info.description for i in items) or goals)
    assert requests == [0] * RECOVERY_ATTEMPTS
    assert submitted == ["1-2"]
    assert not scan.collection.remaining


def _restored_ticket_scan(owner, monkeypatch, first_page, refetched, *, prefer_complete=False):
    """
    第 0 页的候选带临时票据（不入检查点），恢复后由 refetched 模拟重取原页的结果。
    """
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.chain.search.scan import ScanPage

    for item in first_page:
        item.enclosure = "https://site.example/download?ticket=do-not-store"
    pages(owner, monkeypatch, {0: first_page})
    initial = SearchScan(owner=owner, plan=plan([1, 2]), full=True, prefer_complete=prefer_complete)
    source = next(key for key in initial.queries if not key.startswith("plugin:"))
    initial.step(source)
    initial.step(source)
    initial.step("plugin:Show")
    monkeypatch.setattr(owner, "run_module", lambda _method, **params: TorrentInfo(**params["record"]))
    fetched = []
    def fetch(_sources, _key, page, **_params):
        fetched.append(page)
        return ScanPage(refetched, SiteSearchObservation(True, "success", raw_count=len(refetched)))
    monkeypatch.setattr(SearchSources, "fetch", fetch)
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(initial.state()))
    return SearchScan(owner=owner, plan=plan([1, 2]), full=True, prefer_complete=prefer_complete,
                      snapshot=snapshot), fetched


@pytest.mark.parametrize("outcome", ["cooldown", "error"])
@pytest.mark.parametrize("recovery_succeeds", [False, True])
def test_complete_pack_recovery_waits_across_checkpoints_before_partial_fallback(
    owner, monkeypatch, scan_clock, outcome, recovery_succeeds,
):
    """
    整包恢复等待期间不提交分集；检查点保留重试次数，恢复成功或达到上限后才能推进。
    """
    pack = TorrentInfo(site=1, description="pack", title="Show S01 Complete 2026 1080p")
    scan, _ = _restored_ticket_scan(owner, monkeypatch, [torrent(1), pack], [], prefer_complete=True)
    part_key = next(key for key, saved in scan.candidates.items() if saved["torrent"]["description"] == "1-1")
    scan.candidates[part_key]["torrent"]["enclosure"] = "https://site.example/download?id=1"
    calls = []

    def fetch(_sources, _key, page, **_params):
        """
        整包原页前两次受限或失败，第三次可恢复，也可继续失败到本轮放弃。
        """
        calls.append(page)
        if recovery_succeeds and len(calls) == 3:
            restored = TorrentInfo(site=1, description="pack", title="Show S01 Complete 2026 1080p",
                                   enclosure="https://site.example/download?id=pack")
            return ScanPage([torrent(1), restored], SiteSearchObservation(True, "success", raw_count=2))
        if outcome == "cooldown":
            retry_at = (scan_clock["now"] + timedelta(hours=1)).isoformat(timespec="seconds")
            return ScanPage([], SiteSearchObservation(), retry_at, "cooldown")
        return ScanPage([], SiteSearchObservation(True, "error", error="timeout"))

    monkeypatch.setattr(SearchSources, "fetch", fetch)
    submitted = []

    def submit(items, goals):
        """
        记录恢复等待结束后首次允许交给下载层的候选。
        """
        submitted.append(({item.torrent_info.description for item in items}, goals))
        return goals

    for version in range(2):
        with pytest.raises(SubscriptionSearchDeferred) as error:
            scan.run(submit)
        assert submitted == []
        snapshot = SearchSessionSnapshot("round", version, encode_search_state(scan.state()))
        scan = SearchScan(owner=owner, plan=plan([1, 2]), full=True, prefer_complete=True, snapshot=snapshot)
        assert not scan.restart
        scan_clock["now"] = datetime.fromisoformat(error.value.retry_at) + timedelta(seconds=1)

    scan.run(submit)

    assert calls == [0, 0, 0]
    assert submitted == ([({"1-1", "pack"}, {"1:1", "1:2"})] if recovery_succeeds else [({"1-1"}, {"1:1"})])


@pytest.mark.parametrize("still_listed", [False, True])
def test_several_candidates_vanishing_from_one_refetched_page_do_not_abort_the_round(owner, monkeypatch, still_listed):
    # A、B 同在原页且都需重取票据；重取后两者都不在原页（原页为空或被其他资源挤占），不能抛 KeyError。
    """
    重取页面后多个候选消失时只移除候选，不中断整轮搜索。
    """
    other = [TorrentInfo(site=1, description="other", title="Other Show S01E09 2026 1080p",
                         enclosure="https://site.example/download?id=99")] if still_listed else []
    scan, fetched = _restored_ticket_scan(owner, monkeypatch, [torrent(1), torrent(2)], other)
    submitted = []
    scan.choose({"1:1", "1:2"}, lambda items, ready: submitted.extend(items) or set())
    assert fetched == [0]
    assert submitted == []
    assert not scan.candidates


def test_recovered_candidate_is_filtered_again_before_submission(owner, monkeypatch):
    # 检查点保存时免费，恢复票据时免费已结束：恢复后的资源必须重新过滤，不能提交。
    """
    恢复得到的候选在提交前重新经过过滤规则。
    """
    free = torrent(1)
    free.downloadvolumefactor = 0
    ended = torrent(1)
    ended.downloadvolumefactor = 1
    ended.enclosure = "https://site.example/download?ticket=fresh"
    scan, _ = _restored_ticket_scan(owner, monkeypatch, [free], [ended])
    scan.plan = replace(scan.plan, candidate_filter=lambda items: [
        item for item in items if item.torrent_info.downloadvolumefactor == 0])
    submitted = []
    scan.choose({"1:1"}, lambda items, ready: submitted.extend(items) or set())
    assert submitted == []


class _StubSiteBudget(SubscriptionSiteBudget):
    """
    按站点模拟预算：故障站点始终长冷却推迟，健康站点立即放行。
    """

    def __init__(self, deferred_site_id: int, retry_seconds: float) -> None:
        """
        保存故障站点和每次预算等待的秒数，不连接预算数据库。
        """
        super().__init__(repository=None, owner="test", cancelled=lambda: False, stop_state=runtime_stop_state)
        self.deferred_site_id = deferred_site_id
        self.retry_seconds = retry_seconds

    def acquire(self, site_id: int, *, minimum_interval: float = 0) -> SiteBudgetClaim:
        """
        故障站点返回冷却时间，其余站点直接取得测试租约。
        """
        if site_id == self.deferred_site_id:
            retry_at = (scan_module.datetime.now(timezone.utc)
                        + timedelta(seconds=self.retry_seconds)).isoformat(timespec="seconds")
            raise SubscriptionSiteBudgetUnavailable(site_id=site_id, retry_at=retry_at, wait_reason="cooldown")
        return SiteBudgetClaim(site_id=site_id, acquired=True, retry_at="",
                               consecutive_failures=0, lease_token="stub")

    def record_request(self, site_id: int, candidate_count: int) -> None:
        """
        测试不落库，忽略请求指标。
        """
        return None

    def record_success(self, site_id: int) -> None:
        """
        测试不落库，忽略成功指标。
        """
        return None

    def finish(self, claim: SiteBudgetClaim, observation, *, deferral_seconds: float = 0) -> bool:
        """
        测试不落库，直接收口租约。
        """
        return True


@pytest.fixture
def scan_clock(monkeypatch):
    """
    使用可推进的 UTC 时钟验证冷却到期，不实际等待或清除重试状态。
    """
    clock = {"now": datetime.now(timezone.utc).replace(microsecond=500000)}

    class Clock(datetime):
        """
        返回测试控制的时刻，保留标准时间文本的解析行为。
        """

        @classmethod
        def now(cls, tz=None):
            """
            返回带指定时区的当前测试时间。
            """
            return clock["now"].astimezone(tz)

    monkeypatch.setattr(scan_module, "datetime", Clock)
    return clock


def test_budget_deferred_dead_site_settles_and_releases_cached_candidates(owner, monkeypatch, scan_clock):
    """
    永久预算冷却的站点在本轮结算后放行"全部来源才提交"的屏障，缓存候选得以提交；
    结算只在本轮有效，下一轮该站点重新参与搜索。
    """
    monkeypatch.setattr(owner, "_sync_indexers",
                        lambda _sites: [{"id": 1, "name": "test", "parser": "mTorrent"},
                                        {"id": 2, "name": "dead", "parser": "mTorrent"}])
    site_pages = {(1, 0): [torrent(1)], (1, 1): []}

    def request(**params):
        """
        健康站点提供候选和末页证据，故障站点由预算拦截。
        """
        values = site_pages.get((params["site"]["id"], params["page"]), [])
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(values))
        return values

    monkeypatch.setattr(owner, "search_site_torrents", request)
    monkeypatch.setattr(owner, "_subscription_site_budget",
                        _StubSiteBudget(deferred_site_id=2, retry_seconds=3600.0), raising=False)

    scan = SearchScan(owner=owner, plan=plan([1]))
    submitted = []

    def submit(contexts, ready):
        """
        记录屏障放行后真正提交的候选。
        """
        submitted.extend(contexts)
        return ready

    # 按保存的重试时间推进时钟，并恢复检查点，模拟持久队列再次唤醒。
    for _ in range(5):
        scan.run(submit)
        if scan.ended:
            break
        if scan.retry:
            scan_clock["now"] = datetime.fromisoformat(min(scan.retry.values())) + timedelta(seconds=1)
        snapshot = SearchSessionSnapshot("round", 0, encode_search_state(scan.state()))
        scan = SearchScan(owner=owner, plan=plan([1]), snapshot=snapshot)

    deferred_keys = [key for key in scan.collection.sources if key.startswith("2:")]
    assert deferred_keys
    assert all(scan.collection.sources[key].failed for key in deferred_keys)
    assert all(scan.collection.sources[key].budget_deferrals >= BUDGET_DEFERRAL_LIMIT for key in deferred_keys)
    assert scan.collection.settled == {"1:1"}
    assert submitted
    assert scan.ended is True


def test_short_budget_wait_does_not_settle_source(owner, monkeypatch):
    """
    秒级预算等待（请求间隔、并发租约）属于健康站点噪声，不得计数或结算来源。
    """
    monkeypatch.setattr(owner, "_subscription_site_budget",
                        _StubSiteBudget(deferred_site_id=1, retry_seconds=11.0), raising=False)

    scan = SearchScan(owner=owner, plan=plan([1]))
    submitted = []

    def submit(contexts, ready):
        """
        记录是否错误放行了仍在短等待的来源。
        """
        submitted.extend(contexts)
        return ready

    for _ in range(5):
        scan.run(submit)
        scan.retry.clear()

    key = next(key for key in scan.collection.sources if key.startswith("1:"))
    source = scan.collection.sources[key]
    assert source.failed is False
    assert source.budget_deferrals == 0
    assert submitted == []
    assert scan.ended is False


def test_exact_long_cooldown_counts_after_timestamp_truncation(owner, monkeypatch, scan_clock):
    """
    预算时间保存到秒，恰好达到长冷却门槛时不能因当前时间的小数部分漏计。
    """
    fixed_now = scan_clock["now"]
    retry_at = (fixed_now + timedelta(seconds=BUDGET_DEFERRAL_SECONDS)).isoformat(timespec="seconds")
    monkeypatch.setattr(SearchSources, "fetch", lambda *_args, **_kwargs: ScanPage(
        [], SiteSearchObservation(), retry_at, "cooldown"))
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.step("1:Show")
    assert scan.collection.sources["1:Show"].budget_deferrals == 1


@pytest.mark.parametrize("ticket_count", [1, 2])
def test_ticket_recovery_long_cooldown_is_bounded_across_checkpoints(owner, monkeypatch, scan_clock, ticket_count):
    """
    缓存候选重取票据也必须有限等待；恢复检查点后保留计数并继续提交可用候选。
    """
    scan, _ = _restored_ticket_scan(owner, monkeypatch, [torrent(1, version)
                                                      for version in range(1, ticket_count + 2)], [])
    backup_key = next(key for key, saved in scan.candidates.items()
                      if saved["torrent"]["description"] == f"1-{ticket_count + 1}")
    scan.candidates[backup_key]["torrent"]["enclosure"] = "https://site.example/download?id=backup"
    requests = []

    def fetch(_sources, _key, page, **_params):
        """
        模拟原页重取持续受站点长冷却阻塞。
        """
        requests.append(page)
        retry_at = (scan_clock["now"] + timedelta(hours=1)).isoformat(timespec="seconds")
        return ScanPage([], SiteSearchObservation(), retry_at, "cooldown")

    monkeypatch.setattr(SearchSources, "fetch", fetch)
    submitted = []

    def submit(items, goals):
        """
        记录实际可用候选并结算目标。
        """
        submitted.extend(item.torrent_info.description for item in items)
        return goals

    for version in range(BUDGET_DEFERRAL_LIMIT - 1):
        with pytest.raises(SubscriptionSearchDeferred) as error:
            scan.run(submit)
        assert error.value.wait_reason == "cooldown"
        assert error.value.site_ids == (1,)
        assert datetime.fromisoformat(error.value.retry_at) > scan_clock["now"] + timedelta(minutes=59)
        snapshot = SearchSessionSnapshot("round", version, encode_search_state(scan.state()))
        scan = SearchScan(owner=owner, plan=plan([1, 2]), full=True, snapshot=snapshot)
        with pytest.raises(SubscriptionSearchDeferred):
            scan.run(submit)
        assert len(requests) == version + 1
        scan_clock["now"] = datetime.fromisoformat(error.value.retry_at) + timedelta(seconds=1)
    scan.run(submit)
    assert len(requests) == BUDGET_DEFERRAL_LIMIT
    assert submitted == [f"1-{ticket_count + 1}"]
    assert scan.ended


def test_successful_ticket_recovery_resets_budget_wait_count(owner, monkeypatch):
    """
    原页成功取回票据后清零等待计数，后续页不继承此前冷却。
    """
    scan, _ = _restored_ticket_scan(owner, monkeypatch, [torrent(1)], [torrent(1)])
    scan.collection.sources["1:Show"].budget_deferrals = BUDGET_DEFERRAL_LIMIT - 1
    scan.run(lambda _items, goals: goals)
    assert scan.collection.sources["1:Show"].budget_deferrals == 0
    assert scan.collection.settled == {"1:1"}


@pytest.mark.parametrize("wait_reason, seconds, expected", [
    ("cooldown", 11, 1), ("busy", 3600, 1), ("cooldown", 3600, 2),
])
def test_budget_wait_count_restores_and_success_resets(owner, monkeypatch, wait_reason, seconds, expected):
    """
    检查点保留长冷却累计，短等待与忙碌不加计数，成功取页后清零。
    """
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.collection.sources["1:Show"].budget_deferrals = 1
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(scan.state()))
    scan = SearchScan(owner=owner, plan=plan([1]), snapshot=snapshot)
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    monkeypatch.setattr(SearchSources, "fetch", lambda *_args, **_kwargs: ScanPage(
        [], SiteSearchObservation(), retry_at, wait_reason))
    scan.step("1:Show")
    assert scan.collection.sources["1:Show"].budget_deferrals == expected
    pages(owner, monkeypatch, {0: []})
    monkeypatch.setattr(SearchSources, "fetch", lambda *_args, **_kwargs: ScanPage(
        [], SiteSearchObservation(True, "success", raw_count=0)))
    scan.step("1:Show")
    assert scan.collection.sources["1:Show"].budget_deferrals == 0
    assert SearchScan(owner=owner, plan=plan([1])).collection.sources["1:Show"].budget_deferrals == 0


def test_legacy_checkpoint_defaults_budget_wait_count_to_zero(owner):
    """
    没有预算计数的旧检查点仍可恢复，不会重启或提前结算来源。
    """
    state = SearchScan(owner=owner, plan=plan([1])).state()
    for source in state["collection"]["sources"].values():
        source.pop("budget_deferrals")
    snapshot = SearchSessionSnapshot("round", 0, encode_search_state(state))
    scan = SearchScan(owner=owner, plan=plan([1]), snapshot=snapshot)
    assert not scan.restart
    assert all(source.budget_deferrals == 0 and not source.failed for source in scan.collection.sources.values())
