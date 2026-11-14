"""
订阅缺集范围不能被下载候选的类型或季号扩大。
"""

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
from app.chain.search.scan import search_targets
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def scope(monkeypatch):
    """
    保留真实缺集计算，只隔离媒体库查询和订阅持久化。
    """
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
    chain._SubscribeChain__apply_subscribe_update = Mock(
        side_effect=lambda current, fields, **_kwargs: replace(current, **fields)
    )
    monkeypatch.setattr(subscribe_refresh, "DownloadChain", lambda: download)
    monkeypatch.setattr(subscribe_policy, "DownloadChain", lambda: download)
    return SimpleNamespace(subscribe=subscribe, media=media, download=download, chain=chain)


@pytest.mark.parametrize("candidate_type", [MediaType.TV, MediaType.MOVIE, MediaType.UNKNOWN, None])
def test_missing_scope_uses_tv_subscription_with_untyped_season(scope, candidate_type):
    """
    无季号候选即使误识别类型，也只能返回订阅季内未下载的目标集。
    """
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
    """
    多季候选不能带入其他季，特别季 S00 也必须保留目标季身份。
    """
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
    """
    整季未入库时，开始集数之后的需求必须成为显式集数，不能保持整季标记。
    """
    scope.download._media_exists_with_server_cache.return_value = None
    subscribe = replace(scope.subscribe, note=[])

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert not satisfied
    assert set(missing["tmdb:223911"]) == {1}
    assert set(missing["tmdb:223911"][1].episodes) == set(range(159, 201))
    assert missing["tmdb:223911"][1].start_episode == 159


@pytest.mark.parametrize("library", [None, ExistMediaInfo(seasons={2: [1]})])
@pytest.mark.parametrize(("total", "expected"), [(1, [1]), (5, [1, 2, 3, 4, 5]), (200, [])])
def test_missing_scope_materializes_full_season_before_total_episode(scope, library, total, expected):
    """
    整季未入库且总集数裁掉季末时，同样转成显式集数；总集数覆盖整季才保留整季标记。

    媒体库完全没有该剧，或只有其他季时，两条缺集分支口径一致。
    """
    scope.download._media_exists_with_server_cache.return_value = library
    subscribe = replace(scope.subscribe, start_episode=1, total_episode=total, note=[])

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert not satisfied
    info = missing["tmdb:223911"][1]
    assert info.episodes == expected
    assert info.start_episode == 1
    assert info.total_episode == total


@pytest.mark.parametrize(("season_episodes", "start", "total", "expected"), [
    (list(range(13, 25)), 13, 6, []),
    (list(range(13, 25)), None, 6, []),
    (list(range(13, 25)), 13, 18, list(range(13, 19))),
    (list(range(13, 25)), 13, 24, list(range(13, 25))),
    (list(range(13, 25)), 13, 10000, []),
    (list(range(1, 11)) + [12], 1, 11, list(range(1, 12))),
    (list(range(1, 11)) + [12], 1, 8, list(range(1, 9))),
])
def test_missing_scope_resolves_manual_total_with_irregular_episode_numbers(
        scope, season_episodes, start, total, expected,
):
    """
    手工末集号覆盖累计编号和断号；早于季首的历史集数配置不能被误判为已完成。
    """
    scope.download._media_exists_with_server_cache.return_value = None
    media = replace(scope.media, seasons={1: season_episodes})
    subscribe = replace(scope.subscribe, start_episode=start, total_episode=total, note=[])

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=media,
    )

    assert not satisfied
    assert missing["tmdb:223911"][1].episodes == expected


@pytest.mark.parametrize("library", [None, ExistMediaInfo(seasons={1: [1]})])
def test_missing_scope_treats_legacy_negative_total_as_unset(scope, library):
    """
    历史负数总集数按未设置处理，一季无入库或部分入库时均保留实际缺集。
    """
    scope.download._media_exists_with_server_cache.return_value = library
    media = replace(scope.media, seasons={1: [1, 2, 3]})
    subscribe = replace(scope.subscribe, start_episode=1, total_episode=-200, note=[])

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=build_subscribe_meta(subscribe), mediainfo=media,
    )

    assert not satisfied
    info = missing["tmdb:223911"][1]
    assert info.total_episode == 3
    assert info.episodes == ([2, 3] if library else [])


def test_missing_scope_applies_manual_end_to_partially_existing_cumulative_season(scope):
    """
    累计编号部分已入库时，E13 至 E18 全部存在即可满足订阅。
    """
    scope.download._media_exists_with_server_cache.return_value = ExistMediaInfo(
        seasons={1: list(range(13, 19))},
    )
    media = replace(scope.media, seasons={1: list(range(13, 25))})
    subscribe = replace(scope.subscribe, start_episode=13, total_episode=18, note=[])

    assert scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=build_subscribe_meta(subscribe), mediainfo=media,
    ) == (True, {})


def test_missing_scope_finishes_only_when_subscription_range_is_satisfied(scope):
    """
    下载历史已覆盖目标范围时，应清空裁剪结果而不是返回更早的库内缺集。
    """
    subscribe = replace(scope.subscribe, note=list(range(159, 201)))

    satisfied, missing = scope.chain.resolve_subscribe_missing(
        subscribe=subscribe, meta=MetaInfo("Renegade Immortal 2023"), mediainfo=scope.media,
    )

    assert satisfied
    assert missing == {}


@pytest.mark.parametrize("accept_downloaded", [False, True])
def test_missing_scope_preserves_episode_best_version_targets(scope, accept_downloaded):
    """
    候选误识别类型时，分集洗版仍按订阅范围和各集优先级计算缺口。
    """
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
    """
    整季洗版仍要求完整覆盖订阅目标范围，不能因候选类型丢失完整覆盖标记。
    """
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
    """
    下载前重算后，旧合集被跳过；跨越开始集的合集只向下载器提交缺失文件。
    """
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


@pytest.mark.parametrize(("episode_label", "season_episodes", "start", "total", "remaining_episodes"), [
    ("S01", list(range(1, 201)), 1, 1, []),
    ("E01-E200", list(range(1, 201)), 1, 1, []),
    ("S01", list(range(1, 11)) + [12], 1, 11, [11]),
    ("S01", list(range(13, 25)), 13, 18, []),
])
@pytest.mark.parametrize("pri_order", [0, 90])
def test_trimmed_total_subscription_only_selects_target_from_season_pack(
        scope, monkeypatch, pri_order, episode_label, season_episodes, start, total, remaining_episodes,
):
    """
    整季包优先与普通回退均只提交订阅范围内的文件，仍缺的集继续等待补集。
    """
    subscribe = replace(scope.subscribe, start_episode=start, total_episode=total, note=[])
    media = replace(scope.media, seasons={1: season_episodes})
    scope.chain.subscription_repository.get.return_value = subscribe
    meta = MetaInfo(f"Renegade Immortal {episode_label} 2023 2160p WEB-DL H265 AAC-UBWEB")
    context = Context(
        meta_info=meta,
        media_info=media,
        torrent_info=TorrentInfo(
            title=meta.org_string, site=1, site_name="TestSite", pri_order=pri_order,
        ),
    )
    download = scope.download
    download._media_exists_with_server_cache.return_value = None
    download.eventmanager = SimpleNamespace(send_event=Mock(return_value=None))
    download._active_download_failure_fingerprints = Mock(return_value={})
    download.download_torrent = Mock(return_value=(b"torrent", None, season_episodes))
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
        subscribe=subscribe, meta=build_subscribe_meta(subscribe), mediainfo=media,
    )
    assert not satisfied

    downloads, remaining = scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context], no_exists=prepared_missing,
        subscribe=subscribe, mediakey="tmdb:223911",
        eligible_targets=search_targets(SimpleNamespace(mediainfo=media, no_exists=prepared_missing)),
    )

    assert downloads == [context]
    if remaining_episodes:
        assert remaining["tmdb:223911"][1].episodes == remaining_episodes
    else:
        assert remaining == {}
    download.download_single.assert_called_once()
    assert download.download_single.call_args.kwargs["episodes"] == set(season_episodes).intersection(
        range(start, total + 1),
    )
