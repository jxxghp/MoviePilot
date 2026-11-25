"""
验证同名媒体搜索结果的身份消歧。
"""

from unittest.mock import Mock

import pytest

from app.chain.search import SearchChain
from app.chain.search import result as search_module
from app.domain.context import MediaInfo, TorrentInfo
from app.runtime.correlation import correlation_scope, get_correlation_id
from app.schemas.types import MediaSource, MediaType


def test_exact_search_rejects_no_year_alias_recognized_as_different_work(monkeypatch):
    """
    精确搜索中的无年份别名候选识别为不同作品时应拒绝。
    """
    target = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="236356",
        tmdb_id=236356,
        title="家族计划",
        original_title="가족계획",
        names=["Family Matters"],
        type=MediaType.TV,
        year="2024",
        original_language="ko",
        season_years={1: "2024"},
    )
    candidate = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="30161",
        tmdb_id=30161,
        title="Family Matters",
        original_title="Family Matters",
        type=MediaType.TV,
        year="2008",
        original_language="en",
    )
    torrent = TorrentInfo(
        title="Family Matters S01 1080p WEBRip DD2.0 x264-TrollHD",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = candidate
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    chain = object.__new__(SearchChain)

    contexts = chain._parse_result(
        torrents=[torrent],
        mediainfo=target,
        rule_groups=[],
    )

    assert contexts == []
    media_chain.recognize_by_meta.assert_called_once()


def test_exact_search_reuses_disambiguation_for_same_parsed_title(monkeypatch):
    """
    同一解析标题的多条资源应复用一次候选识别，避免重复外部查询。
    """
    target = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="236356",
        tmdb_id=236356,
        title="家族计划",
        original_title="가족계획",
        names=["Family Matters"],
        type=MediaType.TV,
        year="2024",
        season_years={1: "2024"},
    )
    candidate = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="236356",
        tmdb_id=236356,
        title="家族计划",
        type=MediaType.TV,
        year="2024",
    )
    torrents = [
        TorrentInfo(
            title=f"Family Matters S01 1080p WEB-DL {group}",
            site_name=f"测试站点{index}",
            category=MediaType.TV.value,
        )
        for index, group in enumerate(("GROUP-A", "GROUP-B"), start=1)
    ]
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = candidate
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    chain = object.__new__(SearchChain)

    contexts = chain._parse_result(
        torrents=torrents,
        mediainfo=target,
        rule_groups=[],
    )

    assert len(contexts) == 2
    media_chain.recognize_by_meta.assert_called_once()


@pytest.fixture
def yearless_evidence():
    """
    构造有明确媒体编号、尚缺首播日期的《铁证》订阅目标。
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="304086",
        tmdb_id=304086,
        title="铁证",
        original_title="铁证",
        names=["Evidence"],
        type=MediaType.TV,
        year=None,
    )


@pytest.mark.parametrize(
    ("season", "resource_year", "candidate_year", "season_years", "kept"),
    [
        pytest.param(1, "2023", "2023", {}, False, id="other-work-same-year"),
        pytest.param(1, "2026", "2023", {}, True, id="fallback-other-year"),
        pytest.param(2, "2026", "2023", {2: "2026"}, False, id="other-work-same-season-year"),
        pytest.param(2, "2026", None, {2: "2026"}, False, id="season-year-only"),
        pytest.param(2, "2026", "2023", {3: "2026"}, True, id="unrelated-season-year"),
        pytest.param(2, "2026", "2026", {2: "2027"}, True, id="known-season-year-takes-precedence"),
    ],
)
def test_exact_search_checks_candidate_year_for_yearless_alias(
    monkeypatch, yearless_evidence, season, resource_year, candidate_year, season_years, kept,
):
    """
    只有二次识别候选也符合资源年份时，不同媒体编号才足以拒绝别名资源。
    """
    candidate = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="330828",
        tmdb_id=330828,
        title="证词",
        original_title="证词",
        type=MediaType.TV,
        year=candidate_year,
        season_years=season_years,
        original_language="zh",
    )
    torrent = TorrentInfo(
        title=f"Evidence S{season:02d} {resource_year} 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    contexts, recognize = _parse_evidence_resources(monkeypatch, yearless_evidence, [torrent], candidate)

    assert bool(contexts) is kept
    recognize.assert_called_once()


def test_exact_search_accepts_yearless_alias_when_identity_is_confirmed(monkeypatch, yearless_evidence):
    """
    目标缺少年份时，二次识别确认同一媒体仍可保留资源。
    """
    torrent = TorrentInfo(
        title="Evidence S01 2026 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )
    contexts, recognize = _parse_evidence_resources(
        monkeypatch, yearless_evidence, [torrent], yearless_evidence,
    )

    assert len(contexts) == 1
    recognize.assert_called_once()


@pytest.mark.parametrize("seasons", [(1, 2), (2, 1)])
def test_exact_search_keeps_alias_recognition_separate_by_season(monkeypatch, yearless_evidence, seasons):
    """
    同名同年的两个季各自识别，交换资源顺序也只保留正确身份的季。
    """
    torrents = [TorrentInfo(
        title=f"Evidence S{season:02d} 2026 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    ) for season in seasons]
    other = MediaInfo(
        media_source=MediaSource.TMDB, media_id="330828", tmdb_id=330828,
        title="证词", type=MediaType.TV, year="2026",
    )

    def recognize_season(meta, *, obtain_images):
        """
        模拟媒体源根据请求季号返回不同身份。
        """
        assert not obtain_images
        return yearless_evidence if meta.begin_season == 1 else other

    contexts, recognize = _parse_evidence_resources(
        monkeypatch, yearless_evidence, torrents, recognize_season=recognize_season,
    )

    assert [context.meta_info.begin_season for context in contexts] == [1]
    assert [call.args[0].begin_season for call in recognize.call_args_list] == list(seasons)


def test_exact_search_skips_disambiguation_without_year_evidence(monkeypatch, yearless_evidence):
    """
    订阅和资源均无年份时继续按标题匹配，不因同名查询引入反证。
    """
    torrent = TorrentInfo(
        title="Evidence S01 1080p WEB-DL H264 AAC",
        site_name="测试站点",
        category=MediaType.TV.value,
    )

    contexts, recognize = _parse_evidence_resources(monkeypatch, yearless_evidence, [torrent])

    assert len(contexts) == 1
    recognize.assert_not_called()


def _parse_evidence_resources(monkeypatch, target, torrents, candidate=None, *, recognize_season=None):
    """
    走精确搜索解析资源，用离线识别结果代替媒体源查询。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta = Mock(return_value=candidate, side_effect=recognize_season)
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    chain = object.__new__(SearchChain)

    contexts = chain._parse_result(
        torrents=torrents,
        mediainfo=target,
        rule_groups=[],
    )

    return contexts, media_chain.recognize_by_meta


def test_parallel_filter_propagates_request_context(monkeypatch):
    """
    按站点并行过滤时，每个 worker 都应保留触发解析的关联 ID。
    """
    target = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1",
        title="测试电影",
        type=MediaType.MOVIE,
        year="2024",
    )
    torrents = [
        TorrentInfo(
            site=index,
            site_name=f"测试站点{index}",
            title=f"测试电影 2024 1080p GROUP-{index}",
            category=MediaType.MOVIE.value,
        )
        for index in (1, 2)
    ]
    observed = []

    def filter_torrent(_self, _torrent, _filter_params):
        """
        保留资源并记录过滤时的请求关联 ID。
        """
        observed.append(get_correlation_id())
        return True

    monkeypatch.setattr(search_module.TorrentHelper, "filter_torrent", filter_torrent)
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "match_torrent",
        staticmethod(lambda **_kwargs: True),
    )
    monkeypatch.setattr(
        search_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    chain = object.__new__(SearchChain)

    with correlation_scope("filter-request"):
        contexts = chain._parse_result(
            torrents=torrents,
            mediainfo=target,
            rule_groups=[],
            filter_params={"free": "true"},
        )

    assert len(contexts) == 2
    assert observed == ["filter-request", "filter-request"]


def _hospital_playlist() -> MediaInfo:
    """
    构造订阅目标《机智的医生生活》：英文名只作为别名出现，资源名通常不带年份。
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="96102",
        tmdb_id=96102,
        title="机智的医生生活",
        original_title="슬기로운 의사생활",
        names=["Hospital Playlist"],
        type=MediaType.TV,
        year="2020",
        original_language="ko",
        season_years={1: "2020", 2: "2021"},
    )


def _parse_with_candidate(monkeypatch, candidate, title="Hospital Playlist S02 1080p NF WEB-DL DDP2.0 x264-DoA"):
    """
    让按名字的再次识别返回指定候选，返回解析后的搜索结果。
    """
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = candidate
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(search_module.TorrentHelper, "sort_torrents", staticmethod(lambda contexts: contexts))
    chain = object.__new__(SearchChain)
    torrent = TorrentInfo(title=title, site_name="测试站点", category=MediaType.TV.value)
    return chain._parse_result(torrents=[torrent], mediainfo=_hospital_playlist(), rule_groups=[])


def test_no_year_alias_accepts_candidate_without_first_air_date(monkeypatch):
    """
    按名字认出的是没有首播日期的残缺重复条目时，不算另一部作品，资源按订阅目标处理。
    """
    candidate = MediaInfo(media_source=MediaSource.TMDB, media_id="290143", tmdb_id=290143,
                          title="Hospital Playlist", original_title="Hospital Playlist",
                          type=MediaType.TV, original_language="tr")
    assert len(_parse_with_candidate(monkeypatch, candidate)) == 1


def test_no_year_alias_accepts_same_year_work_with_different_original_title(monkeypatch):
    """
    同年的同名衍生节目只是原名不同，不足以否定订阅目标，宁可偶尔下错也要能下。
    """
    candidate = MediaInfo(media_source=MediaSource.TMDB, media_id="124625", tmdb_id=124625,
                          title="슬기로운 하드털이", original_title="슬기로운 하드털이",
                          type=MediaType.TV, year="2020", original_language="ko")
    assert len(_parse_with_candidate(monkeypatch, candidate)) == 1


def test_no_year_alias_accepts_when_name_is_not_recognized(monkeypatch):
    """
    按名字识别不到任何作品时没有反证，资源按订阅目标处理。
    """
    assert len(_parse_with_candidate(monkeypatch, None)) == 1
