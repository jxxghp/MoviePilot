"""订阅缺集范围不能被下载候选的类型或季号扩大。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
import app.chain.subscribe.policy as subscribe_policy
import app.chain.subscribe.refresh as subscribe_refresh
from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
from app.chain.download import DownloadChain
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def scope(monkeypatch):
    """保留真实缺集计算，只隔离媒体库查询和订阅持久化。"""
    subscribe = SubscriptionSnapshot(
        id=7,
        name="仙逆",
        year="2023",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="223911",
        season=1,
        start_episode=159,
        total_episode=200,
        manual_total_episode=1,
        best_version=0,
        note=[159, 160],
        state="R",
    )
    media = MediaInfo(
        title="仙逆",
        year="2023",
        type=MediaType.TV,
        media_source=MediaSource.TMDB,
        media_id="223911",
        seasons={0: list(range(1, 11)), 1: list(range(1, 201)), 2: list(range(1, 11))},
    )
    download = object.__new__(DownloadChain)
    download._media_exists_with_server_cache = Mock(return_value=ExistMediaInfo(seasons={1: [159]}))
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=Mock(return_value=subscribe))
    chain.finish_subscribe_or_not = Mock()
    monkeypatch.setattr(subscribe_refresh, "DownloadChain", lambda: download)
    monkeypatch.setattr(subscribe_policy, "DownloadChain", lambda: download)
    return SimpleNamespace(subscribe=subscribe, media=media, download=download, chain=chain)


@pytest.mark.parametrize("candidate_type", [MediaType.TV, MediaType.MOVIE, MediaType.UNKNOWN, None])
def test_missing_scope_uses_tv_subscription_with_untyped_season(scope, candidate_type):
    """无季号候选即使误识别类型，也只能返回订阅季内未下载的目标集。"""
    meta = MetaInfo("Renegade Immortal 2023")
    meta.type = candidate_type

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=scope.subscribe, meta=meta, mediainfo=scope.media,
    )

    assert not satisfied
    assert set(missing) == {"tmdb:223911"}
    assert set(missing["tmdb:223911"]) == {1}
    info = missing["tmdb:223911"][1]
    assert set(info.episodes) == set(range(161, 201))
    assert info.start_episode == 159
    assert info.total_episode == 200
    assert meta.type == candidate_type
    assert meta.begin_season is None
    scope.chain.finish_subscribe_or_not.assert_not_called()


@pytest.mark.parametrize("season", [0, 2])
def test_missing_scope_uses_subscription_season_for_multiseason_candidate(scope, season):
    """多季候选不能带入其他季，特别季 S00 也必须保留目标季身份。"""
    subscribe = replace(scope.subscribe, season=season, start_episode=3, total_episode=10, note=[3])
    meta = MetaInfo("Renegade Immortal S01-S03")

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=meta, mediainfo=scope.media,
    )

    assert not satisfied
    assert set(missing["tmdb:223911"]) == {season}
    info = missing["tmdb:223911"][season]
    assert set(info.episodes) == set(range(4, 11))
    assert info.start_episode == 3
    assert info.total_episode == 10
    assert meta.season_list == [1, 2, 3]


def test_missing_scope_materializes_full_season_after_start_episode(scope):
    """整季未入库时，开始集数之后的需求必须成为显式集数，不能保持整季标记。"""
    scope.download._media_exists_with_server_cache.return_value = None
    subscribe = replace(scope.subscribe, note=[])

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert not satisfied
    assert set(missing["tmdb:223911"]) == {1}
    assert set(missing["tmdb:223911"][1].episodes) == set(range(159, 201))
    assert missing["tmdb:223911"][1].start_episode == 159


def test_missing_scope_finishes_only_when_subscription_range_is_satisfied(scope):
    """下载历史已覆盖目标范围时，应清空裁剪结果而不是返回更早的库内缺集。"""
    subscribe = replace(scope.subscribe, note=list(range(159, 201)))

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert satisfied
    assert missing == {}


@pytest.mark.parametrize("accept_downloaded", [False, True])
def test_missing_scope_preserves_episode_best_version_targets(scope, accept_downloaded):
    """候选误识别类型时，分集洗版仍按订阅范围和各集优先级计算缺口。"""
    subscribe = replace(
        scope.subscribe, best_version=1,
        episode_priority={"159": 100, "160": 80},
    )

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
        best_version_accept_downloaded=accept_downloaded,
    )

    assert not satisfied
    start = 161 if accept_downloaded else 160
    assert set(missing["tmdb:223911"][1].episodes) == set(range(start, 201))
    assert missing["tmdb:223911"][1].start_episode == 159
    scope.download._media_exists_with_server_cache.assert_not_called()


def test_missing_scope_preserves_full_best_version_coverage(scope):
    """整季洗版仍要求完整覆盖订阅目标范围，不能因候选类型丢失完整覆盖标记。"""
    subscribe = replace(scope.subscribe, best_version=1, best_version_full=1, current_priority=80)

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert not satisfied
    info = missing["tmdb:223911"][1]
    assert info.episodes == []
    assert info.start_episode == 159
    assert info.total_episode == 200
    assert info.require_complete_coverage
    scope.download._media_exists_with_server_cache.assert_not_called()


@pytest.mark.parametrize("pack_end", [140, 165])
@pytest.mark.parametrize("note", [[], [159, 160]])
def test_policy_recheck_keeps_scope_through_batch_file_selection(scope, monkeypatch, pack_end, note):
    """下载前重算后，旧合集被跳过；跨越开始集的合集只向下载器提交缺失文件。"""
    subscribe = replace(scope.subscribe, note=note)
    scope.chain.subscription_repository.get.return_value = subscribe
    meta = MetaInfo("Renegade Immortal 2023 Complete 2160p WEB-DL H265 AAC-UBWEB")
    meta.begin_season = 1
    meta.type = MediaType.MOVIE
    context = Context(
        meta_info=meta,
        media_info=scope.media,
        torrent_info=TorrentInfo(title=meta.org_string, site=1, site_name="TestSite"),
    )
    download = scope.download
    download._media_exists_with_server_cache.return_value = ExistMediaInfo(seasons={1: [159, 160]})
    download.eventmanager = SimpleNamespace(send_event=Mock(return_value=None))
    download._active_download_failure_fingerprints = Mock(return_value={})
    download.download_torrent = Mock(return_value=(b"torrent", None, list(range(1, pack_end + 1))))
    download.download_single = Mock(return_value="download-id")
    helper = SimpleNamespace(
        sort_torrents=lambda contexts: contexts,
        get_torrent_episodes=lambda files, **_kwargs: files,
    )
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "_new_torrent_helper", lambda: helper)
    monkeypatch.setattr(download_selection, "_new_torrent_helper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)
    satisfied, prepared_missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=build_subscribe_meta(subscribe), mediainfo=scope.media,
    )
    assert not satisfied
    assert set(prepared_missing["tmdb:223911"][1].episodes) == set(range(161, 201))

    downloads, remaining = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context], no_exists=prepared_missing,
        subscribe=subscribe, mediakey="tmdb:223911",
    )

    scope.chain.finish_subscribe_or_not.assert_not_called()
    assert set(remaining["tmdb:223911"]) == {1}
    if pack_end < 159:
        assert downloads == []
        download.download_single.assert_not_called()
        assert set(remaining["tmdb:223911"][1].episodes) == set(range(161, 201))
    else:
        assert downloads == [context]
        download.download_single.assert_called_once()
        assert download.download_single.call_args.kwargs["episodes"] == set(range(161, 166))
        assert set(remaining["tmdb:223911"][1].episodes) == set(range(166, 201))
