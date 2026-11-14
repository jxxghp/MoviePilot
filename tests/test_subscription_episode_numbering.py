"""
累计集号订阅与默认季内集号校验的兼容回归。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
from app.chain.search.facade import SearchChain
from app.chain.search.result import SearchResultOwner, TorrentHelper
from app.chain.subscribe import match as subscribe_match
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def numbering():
    """
    保留 Issue #7289 的媒体身份、累计范围、识别词与默认季内集数。
    """
    subscribe = SubscriptionSnapshot(
        id=7289, name="航海王", year="1999", type=MediaType.TV.value,
        media_source=MediaSource.TMDB, media_id="37854", season=23,
        start_episode=1156, total_episode=2000, manual_total_episode=1,
        note=list(range(1156, 1180)), state="R",
        custom_words=r"(?i)(One[.\s]Piece.*?)S01E(?=\d{4}) => \1S23E",
    )
    media = MediaInfo(
        media_source=MediaSource.TMDB, media_id="37854", tmdb_id=37854,
        title="航海王", original_title="ワンピース", names=["One Piece"],
        type=MediaType.TV, year="1999", season=23,
        season_years={1: "1999", 23: "2026"}, seasons={1: list(range(1, 62)), 23: list(range(1, 26))},
    )
    torrent = TorrentInfo(
        site=1, site_name="测试站点",
        title="One Piece 1999 S01E1180 2160p WEB-DL H265 AAC-PTerWEB",
        category=MediaType.TV.value,
    )
    missing = {"tmdb:37854": {23: NotExistMediaInfo(
        season=23, episodes=list(range(1180, 2001)), start_episode=1156, total_episode=2000,
    )}}
    return SimpleNamespace(subscribe=subscribe, media=media, torrent=torrent, missing=missing)


@pytest.mark.parametrize("paged", [False, True])
def test_search_accepts_explicit_cumulative_episode_scope(numbering, monkeypatch, paged):
    """
    普通结果解析和逐页身份识别均使用本次缺集范围，并保留原始媒体季内集数。
    """
    scope = numbering
    words = scope.subscribe.custom_words.splitlines()
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda contexts: contexts))
    if paged:
        evidence, contexts = SearchResultOwner._identify_page(
            torrents=[scope.torrent], mediainfo=scope.media,
            targets={f"23:{episode}" for episode in range(1180, 2001)},
            custom_words=words, cache={}, disambiguation={},
        )
        assert evidence[0].targets == frozenset({"23:1180"})
        result = list(contexts.values())
    else:
        result = object.__new__(SearchChain)._parse_result(
            torrents=[scope.torrent], mediainfo=scope.media, rule_groups=[],
            season_episodes={23: scope.missing["tmdb:37854"][23].episodes}, custom_words=words,
        )
    assert len(result) == 1
    assert result[0].meta_info.season_list == [23]
    assert result[0].meta_info.episode_list == [1180]
    assert scope.media.seasons[23] == list(range(1, 26))
    assert scope.torrent.title == "One Piece 1999 S01E1180 2160p WEB-DL H265 AAC-PTerWEB"


@pytest.mark.parametrize(
    ("title", "episodes", "expected"),
    [
        ("One Piece 1999 S23E1180", {23: [1180]}, True),
        ("One Piece 1999 S23E1180", None, False),
        ("One Piece 1999 S23E1180", {23: []}, False),
        ("One Piece 1999 S23E1180", {1: [1180]}, False),
        ("One Piece 1999 S23E2001", {23: list(range(1156, 2001))}, False),
        ("One Piece 1999 S24E1180", {24: [1180]}, False),
        ("One Piece 2000 S23E1180", {23: [1180]}, False),
        ("Other Show 1999 S23E1180", {23: [1180]}, False),
        ("One Piece S23E1180", None, False),
        ("One Piece 1999 S23E01-E25", {23: [1]}, True),
    ],
)
def test_explicit_scope_keeps_identity_and_topology_guards(numbering, title, episodes, expected):
    """
    显式集号只扩展目标季的集数上限，不放宽名称、年份和季号，也不缩小普通合集。
    """
    torrent = TorrentInfo(title=title, site_name="测试站点", category=MediaType.TV.value)
    assert TorrentHelper.match_torrent(
        numbering.media, MetaInfo(title), torrent, season_episodes=episodes,
    ) is expected


@pytest.mark.parametrize("manual", [False, True])
def test_rss_match_keeps_cumulative_subscription_scope(numbering, monkeypatch, manual):
    """
    RSS 标题兜底继续按订阅缺集范围交付累计集号候选，人工标记不改变已有匹配行为。
    """
    scope = numbering
    subscribe = replace(scope.subscribe, manual_total_episode=int(manual))
    candidate = Context(meta_info=MetaInfo(scope.torrent.title), torrent_info=scope.torrent)
    torrents = {"site.example": [candidate]}
    chain = object.__new__(SubscribeChain)
    chain.check_and_handle_existing_media = Mock(return_value=(False, scope.missing))
    chain.get_params = Mock(return_value={})
    chain.filter_torrents = Mock(side_effect=lambda **params: params["torrent_list"])
    chain.finish_subscribe_or_not = Mock()
    chain.subscription_repository = SimpleNamespace(get=Mock(return_value=subscribe))
    download = Mock(return_value=([], scope.missing))
    chain._SubscribeChain__download_best_version_with_full_pack_first = download
    media_chain = SimpleNamespace(
        supplement_media_info=lambda media: media,
        recognize_by_meta=Mock(return_value=None),
    )
    monkeypatch.setattr(subscribe_match, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(subscribe_match, "_prepare_subscription_match", Mock(return_value=(
        build_subscribe_meta(subscribe), scope.media, torrents, [], [],
    )))
    execution = SimpleNamespace(should_stop=lambda: False, report_phase=Mock())

    assert chain._execute_match(
        subscribe=subscribe, processed_torrents=torrents, candidate_index=None,
        fresh_fact_lease=None, execution_context=execution,
    ) == "completed"

    download.assert_called_once()
    matched = download.call_args.kwargs["contexts"]
    assert len(matched) == 1
    assert matched[0].meta_info.episode_list == [1180]
    assert matched[0].meta_info.season_list == [23]
    assert matched[0].media_info_is_target
    assert not matched[0].candidate_recognized
    assert candidate.meta_info.season_list == [1]


def test_requested_scope_preserves_packs_when_episode_count_is_unknown(numbering):
    """
    媒体源没有集数上限时，缺少一集不能把覆盖该集的正常合集当作越界资源。
    """
    numbering.media.seasons[23] = []
    torrent = TorrentInfo(title="One Piece 1999 S23E01-E30", site_name="测试站点")
    assert TorrentHelper.match_torrent(
        numbering.media, MetaInfo(torrent.title), torrent, season_episodes={23: [1]},
    )


@pytest.mark.parametrize("count", [25, 0, None, "unknown"])
@pytest.mark.parametrize("explicit", [False, True])
def test_single_season_total_fallback_keeps_numbering_contract(numbering, count, explicit):
    """
    单季总集数回退只在已知有效上限时约束资源，并允许显式累计集号扩展上限。
    """
    media = numbering.media
    media.seasons = {23: []}
    media.season_years = {23: "1999"}
    media.number_of_episodes = count
    torrent = TorrentInfo(title="One Piece 1999 S23E1180", site_name="测试站点")
    assert TorrentHelper.match_torrent(
        media, MetaInfo(torrent.title), torrent, season_episodes={23: [1180]} if explicit else None,
    ) is (explicit or count != 25)
