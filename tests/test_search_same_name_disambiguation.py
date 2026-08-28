from unittest.mock import Mock

from app.chain.search import SearchChain
from app.chain.search import result as search_module
from app.domain.context import MediaInfo, TorrentInfo
from app.runtime.correlation import correlation_scope, get_correlation_id
from app.schemas.types import MediaSource, MediaType


def test_exact_search_rejects_no_year_alias_recognized_as_different_work(monkeypatch):
    """精确搜索中的无年份别名候选识别为不同作品时应拒绝。"""
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
    """同一解析标题的多条资源应复用一次候选识别，避免重复外部查询。"""
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


def test_parallel_filter_propagates_request_context(monkeypatch):
    """按站点并行过滤时，每个 worker 都应保留触发解析的关联 ID。"""
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
