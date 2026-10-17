"""
验证种子推送与订阅候选的匹配规则。
"""

from types import SimpleNamespace

from app.chain.torrents import TorrentsChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


def _chain() -> TorrentsChain:
    """
    构造不触发外部依赖初始化的种子链实例。
    """
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
    """
    缓存候选返回独立深拷贝，后续识别修改不得污染原始缓存。
    """
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
    """
    候选明确声明的季与订阅目标冲突时，将其排除。
    """
    source = _ctx(media_season=2, begin_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_cache_candidates_keep_multi_season_candidate_covering_target(monkeypatch):
    """
    多季候选覆盖目标季时保留候选及其识别来源。
    """
    source = _ctx(media_season=None, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "themoviedb"


def test_cache_candidates_keep_multi_season_candidate_when_media_season_is_range_start(monkeypatch):
    """
    识别媒体的季仅表示合集起始季时，仍保留覆盖后续目标季的候选。
    """
    source = _ctx(media_season=1, begin_season=1, end_season=2)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(season=2), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "themoviedb"


def test_cache_candidates_ignore_default_meta_season_list_when_no_explicit_meta_season(monkeypatch):
    """
    没有显式季信息时忽略元数据默认季列表，避免误判季冲突。
    """
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
            """
            模拟没有显式季信息却默认返回第一季的解析结果。
            """
            return [1]

    source = _ctx(media_season=2, begin_season=None)
    source.meta_info = _MetaWithDefaultSeasonList()
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    result = _chain().get_subscribe_cache_candidates(_subscribe(season=2), stype="rss")

    assert len(result) == 1
    assert result[0].match_source == "themoviedb"


def test_title_fallback_requires_explicit_flag(monkeypatch):
    """
    标题匹配仅在显式允许时启用，默认不作为身份匹配的替代。
    """
    source = _ctx(media_source=None, media_id=None)
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(_subscribe(), stype="rss") == []


def test_title_fallback_is_diagnostic_only_and_uses_target_media(monkeypatch):
    """
    标题回退只记录诊断匹配，并使用订阅目标媒体身份。
    """
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
    """
    标题相同但元数据类型与订阅目标冲突时，不接受回退候选。
    """
    source = _ctx(media_source=None, media_id=None, meta_type=MediaType.MOVIE)
    source.media_info.type = None
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_explicit_conflicting_identity(monkeypatch):
    """
    候选已有明确且不同的媒体身份时，标题回退不能覆盖该冲突。
    """
    source = _ctx(media_id="999")
    source.match_source = "themoviedb"
    monkeypatch.setattr(TorrentsChain, "get_torrents", lambda self, stype=None: {"site": [source]})

    assert _chain().get_subscribe_cache_candidates(
        _subscribe(),
        stype="rss",
        allow_title_match=True,
    ) == []


def test_title_fallback_rejects_meta_explicit_conflicting_identity(monkeypatch):
    """
    原始元数据带有明确身份冲突时，同样拒绝标题回退。
    """
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
