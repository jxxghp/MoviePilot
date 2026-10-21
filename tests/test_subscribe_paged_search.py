"""
逐页提交复用完整缺集和原下载选集规则，不把批次完成当作订阅完成。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.existence as download_existence
import app.chain.download.selection as download_selection
import app.chain.subscribe.policy as subscribe_policy
import app.chain.subscribe.refresh as subscribe_refresh
import app.chain.subscribe.search as subscribe_search
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
from app.application.subscription.priority import (
    build_full_pack_first_no_exists,
    should_prefer_full_pack_for_initial_download,
)
from app.chain.download import DownloadChain
from app.chain.search.facade import SearchChain
from app.chain.search.result import TorrentHelper
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.metadata import SubscriptionSearchTarget
from app.chain.subscribe.search import (
    _best_version_target_episodes,
    _build_subscription_candidate_filter,
    _finish_paged_subscription,
    _process_paged_subscription,
)
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo, NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType, SystemConfigKey


@pytest.fixture
def subscription(monkeypatch):
    """
    仅隔离媒体库、站点、下载器及订阅写入，保留实际缺集、选集和完成判断。
    """
    initial = SubscriptionSnapshot(id=31, name="Example Show", type=MediaType.TV.value,
                                   media_source=MediaSource.TMDB.value, media_id="1", season=1,
                                   start_episode=1, total_episode=1, manual_total_episode=1, state="R")
    current = {"value": initial}
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=lambda _sid: current["value"])
    chain.eventmanager = SimpleNamespace(send_event=Mock(return_value=None))
    monkeypatch.setattr(chain, "_SubscribeChain__apply_subscribe_update",
                        lambda item, fields, **_params: current.update(value=replace(item, **fields)) or current["value"])
    monkeypatch.setattr(chain, "_SubscribeChain__refresh_subscribe_progress_with_no_exists",
                        Mock(wraps=chain._SubscribeChain__refresh_subscribe_progress_with_no_exists))
    finished = Mock()
    monkeypatch.setattr(chain, "_SubscribeChain__finish_subscribe", finished)
    download = object.__new__(DownloadChain)
    download._media_exists_with_server_cache = Mock(return_value=ExistMediaInfo(seasons={}))
    download.eventmanager = SimpleNamespace(send_event=Mock(return_value=None))
    download._active_download_failure_fingerprints = Mock(return_value={})
    download.download_single = Mock(return_value="hash")
    download.download_torrent = Mock(return_value=(b"torrent", None, [1, 2]))
    helper = SimpleNamespace(sort_torrents=lambda items: items, get_torrent_episodes=lambda files, **_params: files)
    monkeypatch.setattr(download_batch, "_new_torrent_helper", lambda: helper)
    monkeypatch.setattr(download_selection, "_new_torrent_helper", lambda: helper)
    monkeypatch.setattr(subscribe_policy, "DownloadChain", lambda: download)
    monkeypatch.setattr(subscribe_refresh, "DownloadChain", lambda: download)
    media = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1, type=MediaType.TV,
                      title="Example Show", original_title="Example Show", names=["Example Show"],
                      season=1, seasons={1: [1, 2]})
    monkeypatch.setattr(download_existence, "MediaChain",
                        lambda: SimpleNamespace(recognize_media=Mock(return_value=media)))
    return SimpleNamespace(chain=chain, current=current, initial=initial, media=media,
                           download=download, finished=finished)


def _missing(scope, item):
    """
    通过真实订阅规则计算媒体库与下载记录合并后的缺集。
    """
    return scope.chain.resolve_subscribe_missing(
        subscribe=item, meta=build_subscribe_meta(item), mediainfo=scope.media, mediakey="tmdb:1")[1]


def test_full_pack_candidate_without_torrent_cannot_submit_download(subscription, monkeypatch):
    """
    整包候选缺少种子信息时不比较优先级或提交下载，保留原始缺集范围。
    """
    scope = subscription
    monkeypatch.setattr(scope.chain, "subscription_repository", SimpleNamespace())
    context = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media)
    missing = {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=[1], total_episode=1, start_episode=1)}}
    downloads, remaining = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context], no_exists=missing, subscribe=scope.initial, mediakey="tmdb:1",
    )
    assert downloads == []
    assert remaining == missing
    scope.download.download_single.assert_not_called()


def test_paged_search_keeps_new_missing_episodes_in_completion(subscription, monkeypatch):
    """
    搜索中扩展总集数后，下载结算保留新缺集，检查阶段可先同步旧范围。
    """
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    search = SearchChain()
    monkeypatch.setattr(search, "_sync_indexers", lambda _sites: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(search, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(search, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(scope.chain, "get_sub_sites", lambda _subscribe: [1])
    calls = []
    def request(**params):
        """
        搜索首轮后模拟用户扩展订阅范围。
        """
        calls.append(params["page"])
        # 开始搜索后用户将总集数从 1 增加到 2。
        scope.current["value"] = replace(scope.current["value"], total_episode=2)
        items = [] if params["page"] else [TorrentInfo(site=1, site_name="A", title="Example Show S01E01",
                                                      enclosure="https://site.example/download?id=1", pri_order=100)]
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items
    monkeypatch.setattr(search, "search_site_torrents", request)
    _process_paged_subscription(scope.chain, scope.initial, search, target, SystemConfigKey.SubscribeFilterRuleGroups, None)
    assert calls == [0, 1]
    scope.download.download_single.assert_called_once()
    scope.finished.assert_not_called()
    assert _missing(scope, scope.current["value"])["tmdb:1"][1].episodes == [2]
    completion_calls = [
        call for call in scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.call_args_list
        if call.kwargs.get("scene") == "download"
    ]
    assert completion_calls
    for call in completion_calls:
        assert call.kwargs["no_exists"]["tmdb:1"][1].episodes == [2]


def test_best_version_search_uses_normal_rules_for_never_downloaded_episodes(subscription, monkeypatch):
    """
    洗版候选同时包含缺集和升级集时，缺集使用普通订阅规则组。
    """
    scope = subscription
    subscribe = replace(
        scope.initial,
        best_version=1,
        total_episode=2,
        episode_priority={"1": 80},
    )
    target = SubscriptionSearchTarget(
        subscribe,
        build_subscribe_meta(subscribe),
        scope.media,
        {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=[1, 2], total_episode=2)}},
        "tmdb:1",
    )
    contexts = [
        Context(meta_info=MetaInfo("Example Show S01E01"), media_info=scope.media,
                torrent_info=TorrentInfo(site=1, title="Example Show S01E01")),
        Context(meta_info=MetaInfo("Example Show S01E02"), media_info=scope.media,
                torrent_info=TorrentInfo(site=1, title="Example Show S01E02")),
        Context(meta_info=None, media_info=scope.media,
                torrent_info=TorrentInfo(site=1, title="Example Show S01")),
    ]
    monkeypatch.setattr(
        subscribe_search,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: {
            SystemConfigKey.SubscribeFilterRuleGroups: ["normal"],
            SystemConfigKey.BestVersionFilterRuleGroups: ["best"],
        }.get(key)),
    )
    monkeypatch.setattr(SearchChain, "_filter_identified_contexts", lambda _owner, items, *_args: items)
    calls = []
    scope.chain._filter_search_contexts = lambda _subscribe, items, *, apply_best_version=True: (
        calls.append((items, apply_best_version)) or items
    )

    candidate_filter = _build_subscription_candidate_filter(scope.chain, SearchChain(), subscribe, target)
    assert {
        item.meta_info.episode_list[0]
        for item in candidate_filter(contexts)
        if item.meta_info
    } == {1, 2}
    assert [(items[0].meta_info.episode_list, apply_best) for items, apply_best in calls] == [
        ([2], False),
        ([1], True),
    ]



@pytest.mark.parametrize("full_upgrade", [0, 1])
def test_best_version_candidate_filter_handles_full_pack_and_explicit_groups(subscription, monkeypatch, full_upgrade):
    """
    显式规则组同时覆盖两种候选，全集洗版始终执行升级校验。
    """
    scope = subscription
    subscribe = replace(scope.initial, best_version=1, best_version_full=full_upgrade,
                        total_episode=2, filter_groups=["custom"])
    target = SubscriptionSearchTarget(
        subscribe,
        build_subscribe_meta(subscribe),
        scope.media,
        {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=[], total_episode=2)}},
        "tmdb:1",
    )
    calls = []
    scope.chain._filter_search_contexts = lambda _subscribe, items, *, apply_best_version=True: (
        calls.append((items, apply_best_version)) or items
    )
    monkeypatch.setattr(SearchChain, "_filter_identified_contexts", lambda _owner, items, *_args: items)

    candidate_filter = _build_subscription_candidate_filter(scope.chain, SearchChain(), subscribe, target)
    full_pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                        torrent_info=TorrentInfo(site=1, title="Example Show S01"))
    assert candidate_filter([full_pack]) == [full_pack]
    assert calls[0][1] is bool(full_upgrade)
    assert subscribe_search._subscription_rule_groups(subscribe) == (["custom"], ["custom"])
    assert subscribe_search._best_version_target_episodes(subscribe, target) == {1, 2}


@pytest.mark.parametrize("history", [{}, {"episode_priority": {"1": 80}}, {"note": [1, 2]}])
@pytest.mark.parametrize("has_upgrade", [False, True])
@pytest.mark.parametrize("pack_scope", ["S01 Complete", "S01E01-E02"])
def test_full_best_version_search_does_not_fall_back_to_normal_rules(
    subscription: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    history: dict[str, object],
    has_upgrade: bool,
    pack_scope: str,
) -> None:
    """
    全集洗版拒绝仅普通规则放行的整包，无合格升级时不提交下载或推进进度。

    覆盖无下载记录、部分已下载、全部已下载及显式集数与整季标题，保留真实分页和下载选集。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2, manual_total_episode=2,
                      best_version=1, best_version_full=1, current_priority=0, **history)
    scope.current["value"] = current
    target = SubscriptionSearchTarget(current, build_subscribe_meta(current), scope.media,
                                      _missing(scope, current), "tmdb:1")
    search = SearchChain()
    monkeypatch.setattr(search, "_sync_indexers", lambda _sites: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(search, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(search, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(scope.chain, "get_sub_sites", lambda _subscribe: [1])
    monkeypatch.setattr(
        subscribe_search,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: {
            SystemConfigKey.SubscribeFilterRuleGroups: ["normal"],
            SystemConfigKey.BestVersionFilterRuleGroups: ["best"],
        }.get(key)),
    )

    def filter_rules(
        *, rule_groups: list[str], torrent_list: list[TorrentInfo], mediainfo: MediaInfo,
    ) -> list[TorrentInfo]:
        """
        在模块边界模拟普通规则接受 1080p、洗版规则只接受杜比视界。
        """
        assert mediainfo.type == MediaType.TV
        accepted = []
        for torrent in torrent_list:
            if rule_groups == ["normal"] or "DoVi" in torrent.title:
                torrent.pri_order = 100
                accepted.append(torrent)
        return accepted

    rules = Mock(side_effect=filter_rules)
    monkeypatch.setattr(search, "filter_torrents", rules)

    def request(**params: object) -> list[TorrentInfo]:
        """
        返回普通规则可接受的低画质整包，以及可选的合格升级整包。
        """
        titles = [f"Example Show {pack_scope} 1080p WEB-DL H.264"]
        if has_upgrade:
            titles.append(f"Example Show {pack_scope} 2160p DoVi WEB-DL H.265")
        items = [TorrentInfo(site=1, site_name="A", title=title,
                             enclosure=f"https://site.example/download?id={index}")
                 for index, title in enumerate(titles)] if params["page"] == 0 else []
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items

    monkeypatch.setattr(search, "search_site_torrents", request)
    _process_paged_subscription(scope.chain, current, search, target, SystemConfigKey.BestVersionFilterRuleGroups, None)

    assert rules.call_count > 0
    assert all(call.kwargs["rule_groups"] == ["best"] for call in rules.call_args_list)
    if has_upgrade:
        scope.download.download_single.assert_called_once()
        call = scope.download.download_single.call_args
        context = call.args[0] if call.args else call.kwargs["context"]
        assert "DoVi" in context.torrent_info.title
        assert context.confirmed_full_coverage
        assert scope.current["value"].current_priority == 100
        scope.finished.assert_called()
    else:
        scope.download.download_single.assert_not_called()
        assert scope.current["value"].current_priority == 0
        assert scope.current["value"].note == current.note
        assert scope.current["value"].episode_priority == current.episode_priority
        scope.finished.assert_not_called()


def test_non_best_version_candidate_filter_keeps_existing_filtering(subscription, monkeypatch):
    """
    普通订阅仍通过原有候选过滤器，不构造洗版规则分支。
    """
    scope = subscription
    target = SubscriptionSearchTarget(
        scope.initial,
        build_subscribe_meta(scope.initial),
        scope.media,
        {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=[1], total_episode=1)}},
        "tmdb:1",
    )
    filtered = object()
    scope.chain._filter_search_contexts = lambda _subscribe, items, **_kwargs: filtered
    candidate_filter = _build_subscription_candidate_filter(scope.chain, SearchChain(), scope.initial, target)
    assert candidate_filter([]) is filtered


def test_paged_best_version_search_builds_dynamic_rule_checkpoint(subscription, monkeypatch):
    """
    洗版分页计划保存普通与升级规则快照，恢复时配置变化会重新搜索。
    """
    scope = subscription
    subscribe = replace(scope.initial, best_version=1, total_episode=2, episode_priority={"1": 80})
    target = SubscriptionSearchTarget(
        subscribe,
        build_subscribe_meta(subscribe),
        scope.media,
        {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=[1, 2], total_episode=2)}},
        "tmdb:1",
    )
    captured = {}

    class FakeScan:
        """
        捕获分页计划而不发起站点请求。
        """

        def __init__(self, *, plan, **_kwargs):
            """
            保存待验证的搜索计划。
            """
            captured["plan"] = plan

        def run(self, _submit):
            """
            模拟空分页搜索。
            """

    scope.chain.get_sub_sites = lambda _subscribe: [1]
    scope.chain.get_params = lambda _subscribe: {}
    scope.chain._SubscribeChain__build_full_pack_first_no_exists = lambda *_args, **_kwargs: None
    monkeypatch.setattr(subscribe_search, "SearchScan", FakeScan)
    monkeypatch.setattr(subscribe_search, "_finish_paged_subscription", lambda *_args: None)
    monkeypatch.setattr(subscribe_search, "_subscription_search_strategy", lambda _best_version: "single_page")
    _process_paged_subscription(
        scope.chain,
        subscribe,
        SearchChain(),
        target,
        SystemConfigKey.BestVersionFilterRuleGroups,
        None,
    )
    assert captured["plan"].rule_groups == []
    assert captured["plan"].rule_groups_signature == {
        "normal": [],
        "best": [],
    }

def test_best_version_target_episodes_handles_incomplete_identity(subscription):
    """
    历史订阅缺少媒体键或季号时回退到订阅范围，不访问空键映射。
    """
    target = SubscriptionSearchTarget(
        subscription.initial,
        build_subscribe_meta(subscription.initial),
        subscription.media,
        {},
        None,
    )
    subscribe = replace(subscription.initial, season=None)

    assert _best_version_target_episodes(subscribe, target) == {1}



@pytest.mark.parametrize("explicit_missing", [False, True])
@pytest.mark.parametrize("strategy", ["smart", "full", "single_page"])
def test_whole_season_downloads_late_complete_pack_before_early_parts(subscription, monkeypatch, explicit_missing, strategy):
    """
    整季缺失的真实分页与下载选集链路只提交后页全集包，不抢跑前页散集。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2, manual_total_episode=2)
    scope.current["value"] = current
    target = SubscriptionSearchTarget(current, build_subscribe_meta(current), scope.media,
                                      _missing(scope, current), "tmdb:1")
    assert target.missing["tmdb:1"][1].episodes == []
    if explicit_missing:
        target.missing["tmdb:1"][1].episodes = [1, 2]
    search = SearchChain()
    monkeypatch.setattr(search, "_sync_indexers", lambda _sites: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(search, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(search, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(scope.chain, "get_sub_sites", lambda _subscribe: [1])
    monkeypatch.setattr(subscribe_search, "_subscription_search_strategy", lambda _best_version: strategy)
    calls = []

    def download(*_args, **_params):
        """
        下载副作用必须晚于真实末页，不因散集关闭证据提前执行。
        """
        assert calls == ([0] if strategy == "single_page" else [0, 1, 2, 3])
        return "hash"

    scope.download.download_single.side_effect = download

    def request(**params):
        """
        两页散集后出现整季包，再以空页确认来源结束。
        """
        page = params["page"]
        calls.append(page)
        titles = {0: "Example Show S01E01", 1: "Example Show S01E02",
                  2: "Example Show S01 Complete"}
        items = [TorrentInfo(site=1, site_name="A", title=titles[page],
                             enclosure=f"https://site.example/download?id={page}", pri_order=100)] if page < 3 else []
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items

    monkeypatch.setattr(search, "search_site_torrents", request)
    _process_paged_subscription(scope.chain, current, search, target, SystemConfigKey.SubscribeFilterRuleGroups, None)
    assert calls == ([0] if strategy == "single_page" else [0, 1, 2, 3])
    scope.download.download_single.assert_called_once()
    call = scope.download.download_single.call_args
    context = call.args[0] if call.args else call.kwargs["context"]
    assert context.torrent_info.title == ("Example Show S01E01" if strategy == "single_page" else "Example Show S01 Complete")
    assert context.selected_episodes is None
    assert scope.current["value"].lack_episode == (1 if strategy == "single_page" else 0)


@pytest.fixture
def ticket_search(subscription, monkeypatch):
    """
    两站整季候选排序低于分集，首次页面无地址，重取原页后才取得下载地址。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=3, manual_total_episode=3)
    scope.current["value"] = current
    scope.media.seasons = {1: [1, 2, 3]}
    target = SubscriptionSearchTarget(current, build_subscribe_meta(current), scope.media,
                                      _missing(scope, current), "tmdb:1")
    search = SearchChain()
    monkeypatch.setattr(search, "_sync_indexers", lambda _sites: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    monkeypatch.setattr(search, "get_search_page_size", lambda **_params: 100)
    monkeypatch.setattr(search, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(scope.chain, "get_sub_sites", lambda _subscribe: [1, 2])
    monkeypatch.setattr(subscribe_search, "_subscription_search_strategy", lambda _best_version: "single_page")
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(
        lambda items: sorted(items, key=lambda item: -item.torrent_info.pri_order)))
    attempts = {1: 0, 2: 0}
    restored = {1: "ready", 2: "ready"}
    events = []

    def request(**params):
        """
        保留站点与资源身份，允许逐个模拟整包消失或重取后仍缺地址。
        """
        site = params["site"]["id"]
        assert params["page"] == 0
        attempts[site] += 1
        recovering = attempts[site] > 1
        events.append(("recover" if recovering else "search", site))
        parts = [TorrentInfo(site=1, site_name="A", title=f"Example Show S01E{episode:02d}",
                             enclosure=f"https://site.example/download?id={episode}", pri_order=100)
                 for episode in (1, 2)] if site == 1 else []
        outcome = restored[site] if recovering else "missing"
        packs = [] if outcome == "gone" else [TorrentInfo(
            site=site, site_name="A" if site == 1 else "B", title="Example Show S01 Complete",
            enclosure=f"https://site.example/download?id=pack{site}" if outcome == "ready" else None,
            pri_order=80 if site == 1 else 60,
        )]
        items = parts + packs
        report_site_search_outcome(attempted=True, outcome="success")
        report_site_search_page(raw_count=len(items))
        return items

    def prepare(torrent, **_params):
        """
        记录种子解析顺序，让测试控制整包真实文件覆盖范围。
        """
        events.append(("torrent", torrent.site))
        return b"torrent", None, [1, 2, 3]

    def download(context, **_params):
        """
        任何下载副作用前，两站整包都必须已恢复或确认不可用。
        """
        assert attempts == {1: 2, 2: 2}
        events.append(("download", context.torrent_info.site))
        return "hash"

    monkeypatch.setattr(search, "search_site_torrents", request)
    scope.download.download_torrent.side_effect = prepare
    scope.download.download_single.side_effect = download
    return SimpleNamespace(scope=scope, search=search, current=current, target=target,
                           attempts=attempts, restored=restored, events=events)


@pytest.mark.parametrize("first_outcome", ["complete", "incomplete", "failed", "gone", "missing"])
def test_initial_download_prepares_all_complete_packs_before_any_partial_submission(ticket_search, first_outcome):
    """
    低排序整包必须恢复后共同参与选择，前一个不完整或下载失败时先尝试后一个整包。
    """
    search = ticket_search
    scope = search.scope
    if first_outcome in {"gone", "missing"}:
        search.restored[1] = first_outcome
    if first_outcome == "incomplete":
        prepare = scope.download.download_torrent.side_effect

        def incomplete(torrent, **params):
            """
            第一整包标题完整但实际只有一集，必须继续尝试另一站点整包。
            """
            content, path, episodes = prepare(torrent, **params)
            return content, path, [1] if torrent.site == 1 else episodes

        scope.download.download_torrent.side_effect = incomplete
    if first_outcome == "failed":
        download = scope.download.download_single.side_effect

        def failed(context, **params):
            """
            第一整包下载器提交失败，保留第二整包成功的真实回退路径。
            """
            result = download(context, **params)
            return None if context.torrent_info.site == 1 else result

        scope.download.download_single.side_effect = failed

    _process_paged_subscription(scope.chain, search.current, search.search, search.target,
                                SystemConfigKey.SubscribeFilterRuleGroups, None)

    contexts = [call.args[0] if call.args else call.kwargs["context"]
                for call in scope.download.download_single.call_args_list]
    assert [context.torrent_info.site for context in contexts] == (
        [1] if first_outcome == "complete" else [1, 2] if first_outcome == "failed" else [2])
    assert all(context.torrent_info.title == "Example Show S01 Complete" for context in contexts)
    assert scope.current["value"].lack_episode == 0
    assert scope.current["value"].best_version == search.current.best_version
    assert scope.current["value"].current_priority == search.current.current_priority
    assert search.events[:4] == [("search", 1), ("search", 2), ("recover", 1), ("recover", 2)]


def test_initial_download_falls_back_to_original_partial_order_after_all_packs_disappear(ticket_search):
    """
    两站整包均不可用后按原规则下载分集，并保留未覆盖的第三集。
    """
    search = ticket_search
    scope = search.scope
    search.restored.update({1: "gone", 2: "gone"})

    _process_paged_subscription(scope.chain, search.current, search.search, search.target,
                                SystemConfigKey.SubscribeFilterRuleGroups, None)

    contexts = [call.args[0] if call.args else call.kwargs["context"]
                for call in scope.download.download_single.call_args_list]
    assert [context.torrent_info.title for context in contexts] == [
        "Example Show S01E01", "Example Show S01E02"]
    assert scope.current["value"].lack_episode == 1
    scope.download.download_torrent.assert_not_called()


@pytest.mark.parametrize("fields,episodes,expected", [
    ({}, [1, 2], True),
    ({"lack_episode": 1}, [1, 2], True),
    ({"note": [1]}, [1, 2], False),
    ({"note": [9]}, [1, 2], False),
    ({"episode_priority": {"1": 60}}, [1, 2], False),
    ({}, [2], False),
    ({"best_version": 1}, [1, 2], False),
    ({"type": MediaType.MOVIE.value}, [1, 2], False),
    ({"total_episode": 0}, [1, 2], False),
    ({"start_episode": 2}, [2], True),
])
def test_initial_pack_uses_download_records_and_current_missing(fields, episodes, expected):
    """
    首次下载以当前缺集事实为准，已有下载或媒体库已有剧集时不能扩大到全集。
    """
    subscribe = SubscriptionSnapshot(id=1, name="Show", type=MediaType.TV.value, season=1, total_episode=2)
    subscribe = replace(subscribe, **fields)
    missing = {"tmdb:1": {1: NotExistMediaInfo(season=1, episodes=episodes, total_episode=2)}}
    assert should_prefer_full_pack_for_initial_download(subscribe, "tmdb:1", missing) is expected
    if not subscribe.best_version:
        assert bool(build_full_pack_first_no_exists(subscribe, "tmdb:1", missing)) is expected


@pytest.mark.parametrize("pack_episodes,pack_success", [(None, False), ([1, 2], True), ([1], True), ([1, 2], False)])
def test_initial_download_prefers_complete_pack_and_falls_back(subscription, pack_episodes, pack_success):
    """
    显式全缺集时整包优先于散集；种子不完整或无整包时仍下载所需散集。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    missing = _missing(scope, current)
    missing["tmdb:1"][1].episodes = [1, 2]
    scope.chain.check_and_handle_existing_media = Mock(return_value=(False, missing))
    parts = [Context(meta_info=MetaInfo(f"Example Show S01E{episode:02d}"), media_info=scope.media,
                     torrent_info=TorrentInfo(site=1, title=f"Example Show S01E{episode:02d}", pri_order=100))
             for episode in (1, 2)]
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=80))
    contexts = parts + ([pack] if pack_episodes is not None else [])

    def prepare(torrent, **_params):
        """
        解析出的剧集与资源标题独立，避免把标题整季误当成真实完整包。
        """
        return b"torrent", None, pack_episodes if torrent is pack.torrent_info else MetaInfo(torrent.title).episode_list

    def submit(context, **_params):
        """
        整包下载器失败后仍允许提交散集，验证真实回退而非空候选。
        """
        return None if context is pack and not pack_success else "hash"

    scope.download.download_torrent.side_effect = prepare
    scope.download.download_single.side_effect = submit
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=contexts, no_exists=missing, subscribe=current, mediakey="tmdb:1")
    assert downloads == ([pack] if pack_episodes == [1, 2] and pack_success else parts)
    assert not lefts
    assert scope.current["value"].best_version == current.best_version
    assert scope.current["value"].episode_priority == current.episode_priority


def test_initial_pack_rechecks_current_download_progress(subscription):
    """
    搜索开始后新增下载记录时，提交前重读快照并恢复按集选择。
    """
    scope = subscription
    prepared = replace(scope.initial, total_episode=2)
    scope.current["value"] = replace(prepared, note=[1])
    part = Context(meta_info=MetaInfo("Example Show S01E02"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01E02", pri_order=100))
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=80))
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[part, pack], no_exists=_missing(scope, prepared), subscribe=prepared, mediakey="tmdb:1")
    assert downloads == [part]
    assert not lefts
    scope.download.download_torrent.assert_not_called()


def test_ready_batch_uses_original_partial_pack_selection_and_returns_full_missing(subscription):
    """
    本批只准 E01 时，原下载链从合集选 E01，返回值仍保留未就绪 E02。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    context = Context(meta_info=MetaInfo("Example Show S01E01-E02"), media_info=scope.media,
                      torrent_info=TorrentInfo(site=1, site_name="A", title="Example Show S01E01-E02", pri_order=100))
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1"})
    assert downloads == [context]
    assert scope.download.download_single.call_args.kwargs["episodes"] == {1}
    assert lefts["tmdb:1"][1].episodes == [2]


def test_ready_batch_cannot_expand_existing_candidate_episode_restriction(subscription):
    """
    已就绪下载批次不得扩大候选原有的允许剧集范围。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    context = Context(meta_info=MetaInfo("Example Show S01E01-E02"), media_info=scope.media,
                      torrent_info=TorrentInfo(site=1, title="Example Show S01E01-E02", pri_order=100),
                      allowed_episodes=set())
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1"})
    assert not downloads
    scope.download.download_single.assert_not_called()
    assert lefts["tmdb:1"][1].total_episode == 2


def test_all_ready_season_pack_with_extra_episode_downloads_whole_pack_like_original(subscription):
    """
    整季缺集全部就绪时不加本批集数限制，与原 MP 一致：整季包多出一集仍整包下载、不做选集。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=100))
    scope.download.download_torrent.return_value = (b"torrent", None, [1, 2, 3])
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[pack], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1", "1:2"})
    assert downloads == [pack]
    assert "episodes" not in scope.download.download_single.call_args.kwargs
    assert not lefts


def test_all_ready_missing_season_keeps_original_whole_pack_priority(subscription):
    """
    整季缺集全部就绪时先下载整季包，不因单集候选排序靠前而改走拆包。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    episode = Context(meta_info=MetaInfo("Example Show S01E01"), media_info=scope.media,
                      torrent_info=TorrentInfo(site=1, title="Example Show S01E01", pri_order=200))
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=100))
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[episode, pack], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1", "1:2"})
    assert downloads == [pack]
    scope.download.download_single.assert_called_once()
    assert "episodes" not in scope.download.download_single.call_args.kwargs
    assert not lefts


@pytest.mark.parametrize("files,accepted", [([1, 2], True), ([1], False)])
def test_full_season_upgrade_keeps_complete_pack_requirement(subscription, files, accepted):
    """
    整季洗版仍走原完整覆盖校验，完整包可下载，不完整包不能降为拆包。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2, best_version=1, best_version_full=1, current_priority=0)
    scope.current["value"] = current
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=100))
    scope.download.download_torrent.return_value = (b"torrent", None, files)
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[pack], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1", "1:2"})
    if accepted:
        assert downloads == [pack]
        assert pack.confirmed_full_coverage
        assert not lefts
    else:
        assert not downloads
        scope.download.download_single.assert_not_called()
        assert lefts["tmdb:1"][1].require_complete_coverage


def test_episode_upgrade_keeps_original_full_pack_first(subscription):
    """
    按集洗版仍优先保留原始完整合集候选。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2, best_version=1, current_priority=0)
    scope.current["value"] = current
    pack = Context(meta_info=MetaInfo("Example Show S01"), media_info=scope.media,
                   torrent_info=TorrentInfo(site=1, title="Example Show S01", pri_order=100))
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[pack], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1", "1:2"})
    assert downloads == [pack]
    assert pack.confirmed_full_coverage
    assert not lefts


@pytest.mark.parametrize("replacement_allowed", [None, set(), {2}])
def test_replacement_candidate_keeps_batch_and_own_episode_limits(subscription, replacement_allowed):
    """
    插件替换候选后，本批范围和新候选自身限制仍须同时满足。
    """
    scope = subscription
    current = replace(scope.initial, total_episode=2)
    scope.current["value"] = current
    original = Context(meta_info=MetaInfo("Example Show S01E01"), media_info=scope.media,
                       torrent_info=TorrentInfo(site=1, title="Example Show S01E01", pri_order=100))
    replacement = Context(meta_info=MetaInfo("Example Show S01E01-E02"), media_info=scope.media,
                          torrent_info=TorrentInfo(site=1, title="Example Show S01E01-E02", pri_order=100),
                          allowed_episodes=replacement_allowed)
    scope.download.eventmanager.send_event.return_value = SimpleNamespace(event_data=SimpleNamespace(
        updated=True, updated_contexts=[replacement], source="replacement-test"))
    downloads, lefts = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[original], no_exists=_missing(scope, current), subscribe=current,
        mediakey="tmdb:1", eligible_targets={"1:1"})
    if replacement_allowed is None:
        assert downloads == [replacement]
        assert scope.download.download_single.call_args.kwargs["episodes"] == {1}
        assert lefts["tmdb:1"][1].episodes == [2]
    else:
        assert not downloads
        scope.download.download_single.assert_not_called()
        assert lefts["tmdb:1"][1].episodes == [1, 2]


def test_scan_end_preserves_newer_automatic_total_and_missing(subscription, monkeypatch):
    """
    结束搜索不能用开始时的两集快照覆盖后来确认的三集，也不能提前完成订阅。
    """
    scope = subscription
    initial = replace(scope.initial, total_episode=2, manual_total_episode=0)
    target = SubscriptionSearchTarget(initial, build_subscribe_meta(initial), scope.media,
                                      _missing(scope, initial), "tmdb:1")
    scope.current["value"] = replace(initial, total_episode=3, note=[1, 2])
    search = SearchChain()
    monkeypatch.setattr(search, "_sync_indexers", lambda _sites: [])
    monkeypatch.setattr(search, "search_plugin_torrents", lambda **_params: [])
    monkeypatch.setattr(scope.chain, "get_sub_sites", lambda _subscribe: [])
    _process_paged_subscription(scope.chain, initial, search, target, SystemConfigKey.SubscribeFilterRuleGroups, None)
    assert scope.current["value"].total_episode == 3
    assert scope.current["value"].lack_episode == 1
    assert _missing(scope, scope.current["value"])["tmdb:1"][1].episodes == [3]
    scope.finished.assert_not_called()


def test_scan_end_reconciles_current_missing_after_target_was_submitted(subscription):
    """
    本轮原目标已提交，但用户增加了总集数，结束扫描时仍须保留新增缺集。
    """
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    scope.current["value"] = replace(scope.initial, note=[1], total_episode=2)
    _finish_paged_subscription(scope.chain, scope.initial, target)
    scope.finished.assert_not_called()
    progress = scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists
    progress.assert_called_once()
    assert progress.call_args.kwargs["no_exists"]["tmdb:1"][1].episodes == [2]


def test_scan_end_cannot_finalize_replaced_subscription(subscription):
    """
    分页扫描结束时，不得完成已被替换的订阅版本。
    """
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    scope.current["value"] = replace(scope.initial, media_id="2")
    _finish_paged_subscription(scope.chain, scope.initial, target)
    scope.finished.assert_not_called()
    scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.assert_not_called()


def test_scan_end_still_reconciles_a_subscription_paused_during_the_accepted_search(subscription):
    """
    与原流程一致：暂停只影响后续自动调度，已接纳的搜索照常结算缺集。
    """
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    scope.current["value"] = replace(scope.initial, state="S", total_episode=2, note=[1])
    _finish_paged_subscription(scope.chain, scope.initial, target)
    progress = scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists
    progress.assert_called_once()
    assert progress.call_args.kwargs["no_exists"]["tmdb:1"][1].episodes == [2]


@pytest.mark.parametrize("media_type", [MediaType.TV, MediaType.MOVIE])
def test_scan_end_still_finishes_when_current_media_is_complete(subscription, media_type):
    """
    扫描结束时当前媒体已完整，仍应正常完成订阅。
    """
    scope = subscription
    current = replace(scope.initial, type=media_type.value, note=[1])
    scope.current["value"] = current
    scope.media.type = media_type
    target = SubscriptionSearchTarget(current, build_subscribe_meta(current), scope.media, {}, "tmdb:1")
    _finish_paged_subscription(scope.chain, current, target)
    scope.finished.assert_called_once()
