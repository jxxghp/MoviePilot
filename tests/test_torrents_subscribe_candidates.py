from types import SimpleNamespace

from app.chain.torrents import TorrentsChain
from app.core.context import Context, MediaInfo, TorrentInfo
from app.schemas.types import MediaType


def _chain() -> TorrentsChain:
    """构造不触发外部依赖初始化的种子链实例。"""
    return object.__new__(TorrentsChain)


def _subscribe(**kwargs):
    defaults = {
        "tmdbid": 100,
        "doubanid": None,
        "season": 1,
        "name": "测试剧",
        "type": MediaType.TV.value,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(
        title: str = "测试剧 S01E05",
        *,
        tmdb_id: int = 100,
        douban_id: str = None,
        meta_tmdbid: int = None,
        meta_doubanid: str = None,
        meta_type=MediaType.TV,
        media_season: int = None,
        begin_season: int = 1,
        end_season: int = None,
) -> Context:
    return Context(
        meta_info=SimpleNamespace(
            title=title,
            name="测试剧",
            type=meta_type,
            tmdbid=meta_tmdbid,
            doubanid=meta_doubanid,
            begin_season=begin_season,
            end_season=end_season,
            begin_episode=5,
            episode_list=[5],
        ),
        media_info=MediaInfo(
            type=MediaType.TV,
            tmdb_id=tmdb_id,
            douban_id=douban_id,
            season=media_season,
        ),
        torrent_info=TorrentInfo(title=title),
        resource_source="rss",
        match_source="tmdbid" if tmdb_id else "unknown",
        candidate_recognized=bool(tmdb_id or douban_id),
        media_info_is_target=False,
    )


def test_cache_candidates_return_deep_copies(monkeypatch):
    source = _ctx()
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss")
    result[0].meta_info.title = "changed"
    result[0].media_info.tmdb_id = 999

    assert result[0] is not source
    assert result[0].meta_info is not source.meta_info
    assert result[0].media_info is not source.media_info
    assert source.meta_info.title == "测试剧 S01E05"
    assert source.media_info.tmdb_id == 100


def test_cache_candidates_reject_season_conflict(monkeypatch):
    source = _ctx(media_season=2, begin_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_cache_candidates_keep_multi_season_candidate_covering_target(monkeypatch):
    source = _ctx(media_season=None, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "tmdbid"


def test_cache_candidates_keep_multi_season_candidate_when_media_season_is_range_start(monkeypatch):
    source = _ctx(media_season=1, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(season=2), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "tmdbid"


def test_cache_candidates_ignore_default_meta_season_list_when_no_explicit_meta_season(monkeypatch):
    class _MetaWithDefaultSeasonList:
        title = "测试剧 E05"
        name = "测试剧"
        type = MediaType.TV
        begin_season = None
        end_season = None

        @property
        def season_list(self):
            return [1]

    source = _ctx(media_season=2, begin_season=None)
    source.meta_info = _MetaWithDefaultSeasonList()
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(season=2), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "tmdbid"


def test_title_fallback_requires_explicit_flag(monkeypatch):
    source = _ctx(tmdb_id=None)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_title_fallback_is_diagnostic_only_and_uses_target_media(monkeypatch):
    source = _ctx(tmdb_id=None)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(doubanid="200"), stype="rss", allow_title_match=True)

    assert len(result) == 1
    assert result[0].match_source == "title"
    assert result[0].candidate_recognized is False
    assert result[0].media_info_is_target is True
    assert result[0].media_info.tmdb_id == 100
    assert result[0].media_info.douban_id == "200"
    assert source.media_info.tmdb_id is None


def test_title_fallback_rejects_meta_type_conflict(monkeypatch):
    source = _ctx(tmdb_id=None, meta_type=MediaType.MOVIE)
    source.media_info.type = None
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_explicit_conflicting_identity(monkeypatch):
    source = _ctx(tmdb_id=999)
    source.match_source = "tmdbid"
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_meta_explicit_conflicting_identity(monkeypatch):
    source = _ctx(tmdb_id=None, meta_tmdbid=999)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []
