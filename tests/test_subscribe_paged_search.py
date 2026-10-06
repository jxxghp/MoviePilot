"""逐页提交复用完整缺集和原下载选集规则，不把批次完成当作订阅完成。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.existence as download_existence
import app.chain.download.selection as download_selection
import app.chain.subscribe.policy as subscribe_policy
import app.chain.subscribe.refresh as subscribe_refresh
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
from app.chain.download import DownloadChain
from app.chain.search.facade import SearchChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.metadata import SubscriptionSearchTarget
from app.chain.subscribe.search import _finish_paged_subscription, _process_paged_subscription
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType, SystemConfigKey


@pytest.fixture
def subscription(monkeypatch):
    """仅隔离媒体库、站点、下载器及订阅写入，保留实际缺集、选集和完成判断。"""
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
    return scope.chain.resolve_subscribe_missing(
        subscribe=item, meta=build_subscribe_meta(item), mediainfo=scope.media, mediakey="tmdb:1")[1]


def test_paged_search_keeps_new_missing_episodes_in_completion(subscription, monkeypatch):
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
    for call in scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.call_args_list:
        assert call.kwargs["no_exists"]["tmdb:1"][1].episodes == [2]


def test_ready_batch_uses_original_partial_pack_selection_and_returns_full_missing(subscription):
    """本批只准 E01 时，原下载链从合集选 E01，返回值仍保留未就绪 E02。"""
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
    """整季缺集全部就绪时不加本批集数限制，与原 MP 一致：整季包多出一集仍整包下载、不做选集。"""
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
    """整季缺集全部就绪时先下载整季包，不因单集候选排序靠前而改走拆包。"""
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
    """整季洗版仍走原完整覆盖校验，完整包可下载，不完整包不能降为拆包。"""
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
    """插件替换候选后，本批范围和新候选自身限制仍须同时满足。"""
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
    """结束搜索不能用开始时的两集快照覆盖后来确认的三集，也不能提前完成订阅。"""
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
    """本轮原目标已提交，但用户增加了总集数，结束扫描时仍须保留新增缺集。"""
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    scope.current["value"] = replace(scope.initial, note=[1], total_episode=2)
    _finish_paged_subscription(scope.chain, scope.initial, target)
    scope.finished.assert_not_called()
    progress = scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists
    progress.assert_called_once()
    assert progress.call_args.kwargs["no_exists"]["tmdb:1"][1].episodes == [2]


@pytest.mark.parametrize("changes", [{"state": "S"}, {"media_id": "2"}])
def test_scan_end_cannot_finalize_paused_or_replaced_subscription(subscription, changes):
    scope = subscription
    target = SubscriptionSearchTarget(scope.initial, build_subscribe_meta(scope.initial), scope.media,
                                      _missing(scope, scope.initial), "tmdb:1")
    scope.current["value"] = replace(scope.initial, **changes)
    _finish_paged_subscription(scope.chain, scope.initial, target)
    scope.finished.assert_not_called()
    scope.chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.assert_not_called()


@pytest.mark.parametrize("media_type", [MediaType.TV, MediaType.MOVIE])
def test_scan_end_still_finishes_when_current_media_is_complete(subscription, media_type):
    scope = subscription
    current = replace(scope.initial, type=media_type.value, note=[1])
    scope.current["value"] = current
    scope.media.type = media_type
    target = SubscriptionSearchTarget(current, build_subscribe_meta(current), scope.media, {}, "tmdb:1")
    _finish_paged_subscription(scope.chain, current, target)
    scope.finished.assert_called_once()
