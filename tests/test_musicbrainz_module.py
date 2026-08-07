from app.core.music import MusicMeta
from app.modules.musicbrainz import MusicBrainzModule
from app.core.config import settings


def test_musicbrainz_cover_domains_are_allowed_by_image_proxy():
    """MusicBrainz 封面及其归档重定向域名应进入图片代理安全列表。"""
    assert "coverartarchive.org" in settings.SECURITY_IMAGE_DOMAINS
    assert "archive.org" in settings.SECURITY_IMAGE_DOMAINS


def test_build_query_uses_structured_music_fields():
    """MusicBrainz 查询应同时使用歌曲、艺术家和专辑条件。"""
    query = MusicBrainzModule._build_query(
        MusicMeta(
            title='Love "Story"',
            artists=["Taylor Swift"],
            album="Fearless",
        )
    )

    assert query == (
        'recording:"Love \\"Story\\"" AND '
        'artist:"Taylor Swift" AND release:"Fearless"'
    )


def test_recording_to_info_maps_musicbrainz_payload():
    """MusicBrainz Recording 应映射为统一 MusicInfo。"""
    info = MusicBrainzModule._recording_to_info(
        {
            "id": "recording-1",
            "title": "Get Lucky",
            "length": 369000,
            "first-release-date": "2013-04-19",
            "isrcs": ["USQX91300105"],
            "artist-credit": [
                {"artist": {"name": "Daft Punk"}},
                {"artist": {"name": "Pharrell Williams"}},
            ],
            "releases": [
                {
                    "title": "Random Access Memories",
                    "status": "Official",
                    "date": "2013-05-17",
                    "artist-credit": [{"artist": {"name": "Daft Punk"}}],
                    "release-group": {
                        "id": "release-group-1",
                        "primary-type": "Album",
                        "secondary-types": [],
                    },
                }
            ],
        }
    )

    assert info is not None
    assert info.source == "musicbrainz"
    assert info.media_id == "recording-1"
    assert info.artists == ["Daft Punk", "Pharrell Williams"]
    assert info.album == "Random Access Memories"
    assert info.album_artist == "Daft Punk"
    assert info.year == 2013
    assert info.duration == 369
    assert info.cover_url.endswith("/release-group-1/front-500")


def test_search_music_normalizes_candidates(monkeypatch):
    """搜索接口应把 MusicBrainz 列表转换为 MusicInfo 候选。"""
    module = MusicBrainzModule()
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda *_args, **_kwargs: {
            "recordings": [{"id": "recording-1", "title": "晴天"}]
        },
    )

    results = module.search_music(MusicMeta(title="晴天"), limit=5)

    assert len(results) == 1
    assert results[0].title == "晴天"


def test_recognize_music_ignores_other_sources(monkeypatch):
    """MusicBrainz 模块不应处理其他元数据源的详情请求。"""
    module = MusicBrainzModule()
    called = False

    def fake_request(*_args, **_kwargs):
        """记录测试中是否发生了不应出现的网络调用。"""
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    assert module.recognize_music("netease", "song-1") is None
    assert called is False


def test_recognize_music_fetches_recording_detail(monkeypatch):
    """MusicBrainz 详情请求应按 Recording ID 返回标准音乐信息。"""
    module = MusicBrainzModule()
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda path, params=None: {
            "id": path.rsplit("/", 1)[-1],
            "title": "晴天",
        },
    )

    result = module.recognize_music("musicbrainz", "recording-1")

    assert result is not None
    assert result.media_id == "recording-1"
    assert result.title == "晴天"
