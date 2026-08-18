from types import SimpleNamespace

from app.application.orchestration.torrents import TorrentsChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


def _chain() -> TorrentsChain:
    """构造不触发外部依赖初始化的种子链实例。"""
    return object.__new__(TorrentsChain)


def _subscribe(**kwargs):
    defaults = {
        "media_source": MediaSource.TMDB,
        "media_id": "100",
        "season": 1,
        "name": "测试剧",
        "type": MediaType.TV.value,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(
        title: str = "测试剧 S01E05",
        *,
        media_source: MediaSource = MediaSource.TMDB,
        media_id: str = "100",
        meta_media_source: MediaSource = None,
        meta_media_id: str = None,
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
            media_source=meta_media_source,
            media_id=meta_media_id,
            begin_season=begin_season,
            end_season=end_season,
            begin_episode=5,
            episode_list=[5],
        ),
        media_info=MediaInfo(
            type=MediaType.TV,
            media_source=media_source,
            media_id=media_id,
            season=media_season,
        ),
        torrent_info=TorrentInfo(title=title),
        resource_source="rss",
        match_source=str(media_source) if media_source and media_id else "unknown",
        candidate_recognized=bool(media_source and media_id),
        media_info_is_target=False,
    )


def test_cache_candidates_return_deep_copies(monkeypatch):
    source = _ctx()
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss")
    result[0].meta_info.title = "changed"
    result[0].media_info.media_id = "999"

    assert result[0] is not source
    assert result[0].meta_info is not source.meta_info
    assert result[0].media_info is not source.media_info
    assert source.meta_info.title == "测试剧 S01E05"
    assert source.media_info.media_id == "100"


def test_cache_candidates_reject_season_conflict(monkeypatch):
    source = _ctx(media_season=2, begin_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_cache_candidates_keep_multi_season_candidate_covering_target(monkeypatch):
    source = _ctx(media_season=None, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "themoviedb"


def test_cache_candidates_keep_multi_season_candidate_when_media_season_is_range_start(monkeypatch):
    source = _ctx(media_season=1, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(season=2), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "themoviedb"


def test_cache_candidates_ignore_default_meta_season_list_when_no_explicit_meta_season(monkeypatch):
    class _MetaWithDefaultSeasonList:
        title = "测试剧 E05"
        name = "测试剧"
        type = MediaType.TV
        media_source = MediaSource.TMDB
        media_id = "100"
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
    assert result[0].match_source == "themoviedb"


def test_title_fallback_requires_explicit_flag(monkeypatch):
    source = _ctx(media_source=None, media_id=None)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_title_fallback_is_diagnostic_only_and_uses_target_media(monkeypatch):
    source = _ctx(media_source=None, media_id=None)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(
        _subscribe(media_source=MediaSource.Douban, media_id="200"),
        stype="rss",
        allow_title_match=True,
    )

    assert len(result) == 1
    assert result[0].match_source == "title"
    assert result[0].candidate_recognized is False
    assert result[0].media_info_is_target is True
    assert result[0].media_info.media_source == MediaSource.Douban
    assert result[0].media_info.media_id == "200"
    assert source.media_info.media_id is None


def test_title_fallback_rejects_meta_type_conflict(monkeypatch):
    source = _ctx(media_source=None, media_id=None, meta_type=MediaType.MOVIE)
    source.media_info.type = None
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_explicit_conflicting_identity(monkeypatch):
    source = _ctx(media_id="999")
    source.match_source = "themoviedb"
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_meta_explicit_conflicting_identity(monkeypatch):
    source = _ctx(
        media_source=None,
        media_id=None,
        meta_media_source=MediaSource.TMDB,
        meta_media_id="999",
    )
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []
