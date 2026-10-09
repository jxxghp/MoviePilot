"""完整页驱动验证实际补集、过滤兜底和逐页识别成本。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.chain.search.execution import MediaSearchPlan
from app.chain.search.facade import SearchChain
from app.chain.search.result import TorrentHelper
from app.chain.search.scan import AUTOMATIC_REQUEST_INTERVAL, SearchScan, SearchSources
from app.domain.context import MediaInfo, TorrentInfo
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def owner(monkeypatch):
    chain = SearchChain()
    monkeypatch.setattr(chain, "_sync_indexers", lambda _sites: [{"id": 1, "name": "test", "parser": "mTorrent"}])
    monkeypatch.setattr(chain, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(chain, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(TorrentHelper, "match_torrent", staticmethod(lambda **_params: True))
    monkeypatch.setattr(TorrentHelper, "requires_identity_disambiguation", staticmethod(lambda **_params: False))
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda items: items))
    return chain


def plan(episodes):
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.TV,
                      title="Show", original_title="Show", names=["Show"], season=1, year="2026")
    return MediaSearchPlan(media, keyword="Show", no_exists={"tmdb:1": {1: NotExistMediaInfo(episodes=episodes)}},
                           rule_groups=[])


def torrent(episode, version=1):
    return TorrentInfo(site=1, description=f"{episode}-{version}", title=f"Show S01E{episode:02d} 2026 1080p",
                       enclosure=f"https://site.example/download?id={episode}{version}")


def pages(owner, monkeypatch, data):
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
    from app.chain.search.scan import search_targets
    season_plan = plan([])
    season_plan.no_exists["tmdb:1"][1].total_episode = 1200
    assert len(search_targets(season_plan)) == 1200
    season_plan.no_exists["tmdb:1"][1].total_episode = 9999
    assert search_targets(season_plan) == {"1:season"}
    assert SearchScan(owner=owner, plan=season_plan).full


def test_no_candidate_filter_drives_full_fallback_from_current_cursor(owner, monkeypatch):
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
    data = {page: [torrent(episode, version) for episode in range(page * 40 + 1, (page + 1) * 40 + 1)
                   for version in (1, 2)] for page in range(30)}
    calls = pages(owner, monkeypatch, data)
    scan = SearchScan(owner=owner, plan=plan(list(range(1, 1201))))
    scan.run(lambda _items, ready: ready)
    assert len(calls) == 31
    assert not scan.collection.remaining
    assert len(scan.identity) == 2400


def test_one_hundred_page_cap_ends_the_round_and_does_not_repeat(owner, monkeypatch):
    # 页面出现更新的集，缺失的 1200 属于站点已收录范围，需要一直翻到上限。
    calls = pages(owner, monkeypatch, {page: [torrent(1300, page + 1)] for page in range(125)})
    scan = SearchScan(owner=owner, plan=plan([1200]))
    scan.run(lambda _items, ready: ready)
    assert calls == list(range(100))
    assert scan.collection.remaining == {"1:1200"}
    scan.run(lambda _items, ready: ready)
    assert len(calls) == 100


def test_full_mode_collects_late_candidates_after_a_gap(owner, monkeypatch):
    calls = pages(owner, monkeypatch, {0: [torrent(1)], 1: [torrent(4)], 2: [torrent(1, 2)]})
    chosen = []
    scan = SearchScan(owner=owner, plan=plan([1]), full=True)
    scan.run(lambda items, ready: chosen.extend(item.torrent_info.description for item in items) or ready)
    assert calls == [0, 1, 2, 3]
    assert set(chosen) == {"1-1", "1-2"}


def test_pack_waits_for_all_covered_goals_and_is_submitted_once(owner, monkeypatch):
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
    pack = TorrentInfo(site=1, description="pack", title="Show S01E01-E04 2026 1080p", enclosure="https://site.example/download?id=pack")
    calls = pages(owner, monkeypatch, {0: [pack, torrent(1)], 1: [torrent(4)], 2: [torrent(9)]})
    batches = []
    scan = SearchScan(owner=owner, plan=plan([1, 4]))
    scan.run(lambda items, ready: batches.append((calls[-1], {item.torrent_info.description for item in items}, ready)) or ready)
    assert batches[0] == (1, {"1-1"}, {"1:1"})
    assert sum("pack" in items for _, items, _ in batches) == 1


def test_cross_site_barrier_keeps_late_opportunistic_candidate(owner, monkeypatch):
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


def test_defer_ignores_expired_retries_and_terminal_sources(owner, monkeypatch):
    """失败或耗尽来源的检查点残留不能覆盖仍活跃来源的冷却时间。"""
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
    """所有冷却都已过期时至少等待一个请求间隔再交还订阅队列。"""
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
    monkeypatch.setattr(owner, "search_site_torrents", lambda **_params: [])
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda *_args: pytest.fail("未发出请求不得提交"))
    assert scan.ended
    assert scan.collection.remaining == {"1:1"}


def test_opaque_ticket_recovery_submits_first_page_before_waiting_and_does_not_repeat_it_after_restart(owner, monkeypatch):
    from datetime import datetime, timedelta, timezone

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
        item = TorrentInfo(**params["record"])
        return item if item.enclosure or params.get("allow_missing") else None
    monkeypatch.setattr(owner, "run_module", restore)
    calls = []
    cooling = {"active": True}
    def fetch(_sources, _key, page, **_params):
        if page == 1 and cooling["active"]:
            retry = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
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
    resumed = SearchScan(owner=owner, plan=plan([2]), full=True,
                         snapshot=SearchSessionSnapshot("round", 1, payload))
    resumed.run(lambda _items, goals: batches.append(goals) or goals)
    assert batches == [{"1:1"}, {"1:2"}]
    assert calls == [0, 1]


def test_one_unidentifiable_resource_does_not_block_the_page(owner, monkeypatch):
    calls = pages(owner, monkeypatch, {0: [TorrentInfo(site=1, description="noise", title=""), torrent(1)]})
    submitted = []
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda items, ready: submitted.extend(item.torrent_info.description for item in items) or ready)
    assert submitted == ["1-1"]
    assert calls == [0, 1]
    assert not scan.collection.remaining


def test_restored_scope_recomputes_targets_for_resources_without_season(owner, monkeypatch):
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
    pages(owner, monkeypatch, {0: [torrent(ep) for ep in range(1, 31)]})
    scan = SearchScan(owner=owner, plan=plan([20]))
    scan.step(next(key for key in scan.queries if not key.startswith("plugin:")))
    assert len(scan.identity) == 30
    state = scan.state()
    assert len(state["candidates"]) == 1
    assert set(state["identity"]) == set(state["candidates"])
    assert state["identity"][next(iter(state["candidates"]))]["targets"] == ["1:20"]


def test_progress_log_summary_reports_page_and_remaining_episodes(owner, monkeypatch):
    pages(owner, monkeypatch, {0: [torrent(ep) for ep in range(16, 38)]})
    scan = SearchScan(owner=owner, plan=plan(list(range(1, 38))))
    for key in list(scan.collection.sources):
        scan.step(key)
    assert scan.progress_text() == "已搜索到第 1 页，已提交：无，仍缺：E01-E37"

def test_site_repeating_the_same_page_ends_the_source_instead_of_looping(owner, monkeypatch):
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
    """新作品即使有相同季集号，也不能继承旧作品的提交事实。"""
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
    """同作品改关键词可从首页重搜，但已提交的集仍不重复下载。"""
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
    """检查点必须携带媒体身份，不再用搜索合同兼容缺少该字段的旧格式。"""
    from app.application.search.session import SearchSessionSnapshot, encode_search_state

    scan = SearchScan(owner=owner, plan=plan([1, 2]))
    state = scan.state()
    del state["media_identity"]
    snapshot = SearchSessionSnapshot("task", 1, encode_search_state(state))
    with pytest.raises(KeyError, match="media_identity"):
        SearchScan(owner=owner, plan=plan([1, 2]), snapshot=snapshot)


def test_finished_round_deletes_checkpoint(owner, monkeypatch, tmp_path):
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
    monkeypatch.setattr(owner, "get_search_page_size", lambda **_params: None)
    calls = pages(owner, monkeypatch, {0: [torrent(2)], 1: [torrent(1)]})
    scan = SearchScan(owner=owner, plan=plan([1]))
    scan.run(lambda _items, ready: ready)
    assert calls == [0]
    assert not any(source.failed for source in scan.collection.sources.values())
    assert "部分来源失败" not in scan.progress_text()


def test_unchanged_checkpoint_is_not_written_again(owner, monkeypatch):
    from types import SimpleNamespace

    from app.application.search.session import SearchSessionSnapshot

    class Repository:
        def __init__(self):
            self.writes = 0

        def create(self, **_params):
            self.writes += 1
            return SearchSessionSnapshot("task", 0, "{}")

        def save(self, *, snapshot, **_params):
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


def _restored_ticket_scan(owner, monkeypatch, first_page, refetched):
    """第 0 页的候选带临时票据（不入检查点），恢复后由 refetched 模拟重取原页的结果。"""
    from app.application.search.session import SearchSessionSnapshot, encode_search_state
    from app.application.site.observation import SiteSearchObservation
    from app.chain.search.scan import ScanPage

    for item in first_page:
        item.enclosure = "https://site.example/download?ticket=do-not-store"
    pages(owner, monkeypatch, {0: first_page})
    initial = SearchScan(owner=owner, plan=plan([1, 2]), full=True)
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
    return SearchScan(owner=owner, plan=plan([1, 2]), full=True, snapshot=snapshot), fetched


@pytest.mark.parametrize("still_listed", [False, True])
def test_several_candidates_vanishing_from_one_refetched_page_do_not_abort_the_round(owner, monkeypatch, still_listed):
    # A、B 同在原页且都需重取票据；重取后两者都不在原页（原页为空或被其他资源挤占），不能抛 KeyError。
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
