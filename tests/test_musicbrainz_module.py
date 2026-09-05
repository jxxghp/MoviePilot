import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.context import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule
from app.runtime.config import settings


def test_recording_search_uses_phrase_before_character_fallback(monkeypatch):
    """完整中文名称命中时不应再执行逐字 OR 查询。"""
    module = MusicBrainzModule()
    queries = []

    def request(_path, params):
        """模拟完整名称检索返回同名录音。"""
        queries.append(params["query"])
        return {"recordings": [{"id": "recording", "title": "晴天"}]}

    monkeypatch.setattr(module, "_request_json", request)
    assert module._search_recordings(MetaMusic(title="晴天"), 30)[0].title == "晴天"
    assert queries == ['recording:"晴天"']


def test_recording_search_keeps_character_query_as_last_resort(monkeypatch):
    """完整短语无结果后保留旧的宽召回能力，不能先用单字占满结果窗口。"""
    module = MusicBrainzModule()
    queries = []

    def request(_path, params):
        """首轮无结果，仅在末级检索式返回候选。"""
        queries.append(params["query"])
        return {"recordings": [] if len(queries) == 1 else [{"id": "recording", "title": "晴天"}]}

    monkeypatch.setattr(module, "_request_json", request)
    assert module._search_recordings(MetaMusic(title="晴天"), 30)
    assert queries == ['recording:"晴天"', 'recording:("晴" OR "天")']


def test_artist_alias_lookup_verifies_identity_in_both_io_modes(monkeypatch):
    """同步和异步别名补充必须校验精确艺术家 ID，拒绝其它艺人的响应。"""
    module = MusicBrainzModule()
    artist_id = "a223958d-5c56-4b2c-a30a-87e357bc121b"
    payload = {"id": artist_id, "name": "周杰倫", "aliases": [{"name": "Jay Chou"}]}
    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(module, "_async_request_json", AsyncMock(return_value=payload))
    expected = ["周杰倫", "Jay Chou"]
    assert module._lookup_artist_aliases([artist_id], []) == expected
    assert asyncio.run(module._async_lookup_artist_aliases([artist_id], [])) == expected
    assert module._artist_alias_values(payload, "other-artist") == []


def test_metadata_ranking_prefers_complete_name_over_partial_character_hit():
    """宽召回之后也应按完整标题与署名排序，避免单字相关候选压过准确目标。"""
    exact = MusicInfo(title="晴天", artists=["周杰倫"])
    unrelated = MusicInfo(title="天", artists=["Other Artist"])
    assert MusicBrainzModule._rank_search_candidates(
        MetaMusic(title="周杰伦 晴天"), [unrelated, exact],
    )[0] is exact


def test_musicbrainz_cover_domains_are_allowed_by_image_proxy():
    """MusicBrainz 封面及其归档重定向域名应进入图片代理安全列表。"""
    assert "coverartarchive.org" in settings.SECURITY_IMAGE_DOMAINS
    assert "archive.org" in settings.SECURITY_IMAGE_DOMAINS


def test_build_query_uses_structured_music_fields():
    """MusicBrainz 查询应同时使用歌曲、艺术家和专辑条件。"""
    query = MusicBrainzModule._build_query(
        MetaMusic(
            title='Love "Story"',
            artists=["Taylor Swift"],
            album="Fearless",
        )
    )

    assert query == (
        'recording:"Love \\"Story\\"" AND '
        'artist:"Taylor Swift" AND release:"Fearless"'
    )


def test_build_query_strips_audio_quality_tokens():
    """资源标题中的格式规格与年份后缀不应污染检索式，繁体统一转简体检索。"""
    query = MusicBrainzModule._build_query(
        MetaMusic(
            title="永遠是朋友(2000) - ALAC [16B-44.1kHz]",
            artists=["毛阿敏"],
        )
    )

    # 完整名称的繁简短语组优先，不把长标题拆成单字 OR。
    assert query == 'recording:("永远是朋友" OR "永遠是朋友") AND artist:"毛阿敏"'


def test_select_candidate_matches_traditional_chinese_title():
    """繁体资源标题应能命中 MusicBrainz 简体条目，不因写法差异失配。"""
    meta = MetaMusic(title="永遠是朋友", artists=["毛阿敏"])
    candidates = [
        MusicInfo(
            media_source="musicbrainz",
            media_id="recording-1",
            title="永远是朋友",
            artists=["毛阿敏"],
        ),
    ]

    selected = MusicBrainzModule._select_candidate(meta, candidates, media_source="musicbrainz")

    assert selected is candidates[0]


@pytest.mark.parametrize("music_type", ["recording", "album"])
def test_recognition_accepts_trusted_title_and_artist_aliases(music_type):
    """目录已经返回的同实体别名也必须用于身份确认，不能仅用于搜索展示排序。"""
    meta = MetaMusic(title="Fine Day", artists=["Jay Chou"])
    candidate = MusicInfo(media_source="musicbrainz", media_id="candidate", music_type=music_type,
                          title="晴天", title_aliases=["Fine Day"], artists=["周杰倫"], artist_aliases=["Jay Chou"])
    if music_type == "album":
        assert MusicBrainzModule._select_album_candidate(meta, [candidate]) is candidate
    else:
        assert MusicBrainzModule._select_candidate(meta, [candidate], "musicbrainz") is candidate


@pytest.mark.parametrize("artist", ["AC/DC", "Earth, Wind & Fire", "Beyoncé"])
@pytest.mark.parametrize("music_type", ["recording", "album"])
def test_recognition_preserves_compound_and_accented_artist_names(artist, music_type):
    """复合艺名的解析拆段和拉丁变音符差异不应导致身份确认漏配。"""
    meta = MetaMusic.parse_query(f"{artist.replace('é', 'e')} - Example Work FLAC")
    candidate = MusicInfo(media_source="musicbrainz", media_id="candidate", music_type=music_type,
                          title="Example Work", artists=[artist])
    if music_type == "album":
        assert MusicBrainzModule._select_album_candidate(meta, [candidate]) is candidate
    else:
        assert MusicBrainzModule._select_candidate(meta, [candidate], "musicbrainz") is candidate


@pytest.mark.parametrize("candidate_title", ["One Tree Hill", "One - Tree Hill", "One (Other Song)"])
def test_recording_recognition_rejects_partial_title_identity(candidate_title):
    """单曲确认与资源匹配一样要求作品名称边界，不能借首词或任意括号剥离误配。"""
    meta = MetaMusic(title="One", artists=["U2"])
    candidate = MusicInfo(media_source="musicbrainz", media_id="other", title=candidate_title, artists=["U2"])
    assert MusicBrainzModule._select_candidate(meta, [candidate], "musicbrainz") is None


@pytest.mark.parametrize("music_type", ["recording", "album"])
@pytest.mark.parametrize("input_version,candidate_version", [(None, "Live"), ("Live", None), ("Live", "Remix")])
def test_recognition_rejects_conflicting_recording_versions(music_type, input_version, candidate_version):
    """同名同艺人的不同录音版本仍是不同目标，不能以普通标题分数自动确认。"""
    meta = MetaMusic(title="Example Work", artists=["Artist"], version=input_version)
    candidate = MusicInfo(media_source="musicbrainz", media_id="candidate", music_type=music_type,
                          title="Example Work", artists=["Artist"], version=candidate_version)
    if music_type == "album":
        assert MusicBrainzModule._select_album_candidate(meta, [candidate]) is None
    else:
        assert MusicBrainzModule._select_candidate(meta, [candidate], "musicbrainz") is None


def test_recording_recognition_keeps_isrc_identity_priority():
    """来源返回相同 ISRC 时保留显式录音身份优先级，不被不完整标题和署名阻断。"""
    meta = MetaMusic(title="Unverified", artists=["Unknown"], isrc="USABC2600001")
    candidate = MusicInfo(media_source="musicbrainz", media_id="recording", title="Real Title",
                          artists=["Artist"], version="Live", isrc="USABC2600001")
    misleading = MusicInfo(media_source="musicbrainz", media_id="other", title="Unverified", artists=["Unknown"])
    assert MusicBrainzModule._select_candidate(meta, [misleading, candidate], "musicbrainz") is candidate


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("music_type", ["recording", "album"])
@pytest.mark.parametrize("has_match", [False, True])
def test_recognition_continues_queries_after_rejected_candidates(monkeypatch, async_mode, music_type, has_match):
    """与资源搜索一致，原始候选不能确认身份时继续后续检索式，而非提前宣告无匹配。"""
    module = MusicBrainzModule()
    module.cache = None
    meta = MetaMusic(title="Example Work", artists=["Artist"])
    queries = []

    def request(_path, params):
        """前一检索式仅有无关作品，下一检索式返回同名同署名实体。"""
        queries.append(params["query"])
        title = "Example Work" if has_match and len(queries) > 1 else "Other Work"
        items_key = "recordings" if music_type == "recording" else "release-groups"
        return {items_key: [{"id": "matched", "title": title, "artist-credit": [{"artist": {"name": "Artist"}}]}]}

    monkeypatch.setattr(module, "_recording_queries", lambda _meta: ["first", "second", "third"])
    monkeypatch.setattr(module, "_album_queries", lambda _meta: ["first", "second", "third"])
    monkeypatch.setattr(module, "_request_json", request)
    monkeypatch.setattr(module, "_async_request_json", AsyncMock(side_effect=request))
    if async_mode:
        result = asyncio.run(module.async_recognize_media(meta=meta, music_type=music_type, cache=False))
    else:
        result = module.recognize_media(meta=meta, music_type=music_type, cache=False)
    if has_match:
        assert result and result.media_id == "matched"
    else:
        assert not result or result.media_id is None
    assert queries == (["first", "second"] if has_match else ["first", "second", "third"])


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("music_type", ["recording", "album"])
def test_catalog_browsing_keeps_unconfirmed_candidates(monkeypatch, async_mode, music_type):
    """手动目录浏览仍展示来源原始候选，自动确认的严格规则不能抹掉浏览结果。"""
    module = MusicBrainzModule()
    meta = MetaMusic(title="Example Work", artists=["Artist"])
    key = "recordings" if music_type == "recording" else "release-groups"
    payload = {key: [{"id": "related", "title": "Other Work"}]}
    sync_request = Mock(return_value=payload)
    async_request = AsyncMock(return_value=payload)
    monkeypatch.setattr(module, "_request_json", sync_request)
    monkeypatch.setattr(module, "_async_request_json", async_request)
    if music_type == "recording":
        result = asyncio.run(module._async_search_recordings(meta, 10)) if async_mode else module._search_recordings(meta, 10)
    else:
        result = asyncio.run(module._async_search_albums(meta, 10)) if async_mode else module._search_albums(meta, 10)
    assert [item.media_id for item in result] == ["related"]
    assert sync_request.call_count == (0 if async_mode else 1)
    assert async_request.await_count == (1 if async_mode else 0)


def test_album_secondary_type_is_version_evidence():
    """专辑来源通过 secondary_types 声明现场版时，与标题和独立版本字段同样参与确认。"""
    candidate = MusicInfo(media_source="musicbrainz", media_id="live-album", music_type="album",
                          title="Example Work", artists=["Artist"], secondary_types=["Live"])
    meta = MetaMusic(title="Example Work", artists=["Artist"])
    assert MusicBrainzModule._select_album_candidate(meta, [candidate]) is None
    meta.version = "Live"
    assert MusicBrainzModule._select_album_candidate(meta, [candidate]) is candidate


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
    assert info.media_source.value == "musicbrainz"
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

    results = module.search_music(MetaMusic(title="晴天"), limit=5)

    assert len(results) == 1
    assert results[0].title == "晴天"


def test_search_music_interleaves_recordings_albums_and_artists(monkeypatch):
    """全局音乐搜索应交错返回三类实体，避免单曲结果挤掉整专和艺术家入口。"""
    module = MusicBrainzModule()
    requested = []

    def fake_request(path, params=None):
        """按 MusicBrainz 实体路径返回可区分的搜索结果。"""
        requested.append((path, params))
        if path == "/recording":
            return {
                "recordings": [
                    {"id": "recording-1", "title": "晴天"},
                    {"id": "recording-2", "title": "轨迹"},
                ]
            }
        if path == "/release-group":
            return {
                "release-groups": [
                    {
                        "id": "album-1",
                        "title": "叶惠美",
                        "primary-type": "Album",
                        "artist-credit": [{"artist": {"id": "artist-1", "name": "周杰伦"}}],
                    },
                    {"id": "album-2", "title": "七里香", "primary-type": "Album"},
                ]
            }
        if path == "/artist":
            return {
                "artists": [
                    {"id": "artist-1", "name": "周杰伦", "type": "Person"},
                    {"id": "artist-2", "name": "Jay Chou", "type": "Person"},
                ]
            }
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    results = module.search_music(
        MetaMusic(title="晴天", artists=["周杰伦"]),
        limit=5,
    )

    assert [item.music_type for item in results] == [
        "recording",
        "album",
        "artist",
        "recording",
        "album",
    ]
    assert results[1].album == "叶惠美"
    assert results[2].title == "周杰伦"
    assert results[2].artists == []
    assert requested[1][1]["query"] == 'releasegroup:"晴天" AND artist:("周杰伦" OR "周杰倫")'
    assert requested[2][1]["query"] == 'artist:("周杰伦" OR "周杰倫")'


def test_file_recognition_searches_recordings_only(monkeypatch):
    """本地音轨识别不得把同名专辑或艺术家候选当成 Recording。"""
    module = MusicBrainzModule()
    requested_paths = []

    def fake_request(path, params=None):
        """记录文件识别实际访问的 MusicBrainz 实体。"""
        requested_paths.append(path)
        return {"recordings": [{"id": "recording-1", "title": "晴天"}]}

    monkeypatch.setattr(module, "_request_json", fake_request)

    result = module.recognize_media(meta=MetaMusic(title="晴天"))

    assert result is not None
    assert result.music_type == "recording"
    assert requested_paths == ["/recording"]


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


def test_recognize_music_falls_back_to_album(monkeypatch):
    """旧调用未给实体类型时保留先单曲后专辑的兼容探测。"""
    module = MusicBrainzModule()
    requested = []

    def fake_request(path, params=None):
        """单曲请求返回空，专辑请求返回最小 Release Group 数据。"""
        requested.append(path)
        if path.startswith("/recording/"):
            return None
        if path.startswith("/release-group/"):
            return {
                "id": "release-group-1",
                "title": "A Night at the Opera",
                "primary-type": "Album",
                "first-release-date": "1975-11-21",
                "artist-credit": [{"artist": {"id": "artist-1", "name": "Queen"}}],
                "releases": [],
            }
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)
    monkeypatch.setattr(MusicBrainzModule, "_request_json", staticmethod(fake_request))

    info = module.recognize_music("musicbrainz", "release-group-1")

    assert info is not None
    assert info.music_type == "album"
    assert info.album_id == "release-group-1"
    assert info.artists == ["Queen"]
    assert info.artist_ids == ["artist-1"]
    assert requested[0].startswith("/recording/")


def test_recognize_music_recording_does_not_probe_album(monkeypatch):
    """显式 Recording ID 未命中时不得继续请求同 ID 的专辑实体。"""
    module = MusicBrainzModule()
    requested = []
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda path, params=None: requested.append(path),
    )

    result = module.recognize_music(
        "musicbrainz",
        "recording-missing",
        music_type=MUSIC_ENTITY_RECORDING,
    )

    assert result is None
    assert requested == ["/recording/recording-missing"]


def test_recognize_music_album_skips_recording_namespace(monkeypatch):
    """显式 Album ID 应直接读取 Release Group，不先探测 Recording。"""
    module = MusicBrainzModule()
    requested = []

    def fake_request(path, params=None):
        """记录请求路径并返回最小专辑详情。"""
        requested.append(path)
        if path.startswith("/release-group/"):
            return {
                "id": "release-group-1",
                "title": "叶惠美",
                "artist-credit": [],
                "releases": [],
            }
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    result = module.recognize_music(
        "musicbrainz",
        "release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
    )

    assert result and result.music_type == MUSIC_ENTITY_ALBUM
    assert requested[0] == "/release-group/release-group-1"
    assert not any(path.startswith("/recording/") for path in requested)


def test_music_album_builds_tracks_and_release_variants(monkeypatch):
    """专辑详情应带上曲目列表、发行版本和 10 分制评分。"""
    module = MusicBrainzModule()

    def fake_request(path, params=None):
        """按路径分别返回 Release Group 与代表性 Release 数据。"""
        if path == "/release-group/release-group-1":
            return {
                "id": "release-group-1",
                "title": "A Night at the Opera",
                "primary-type": "Album",
                "secondary-types": ["Live"],
                "first-release-date": "1975-11-21",
                "rating": {"value": 4.25, "votes-count": 44},
                "genres": [{"name": "rock", "count": 13}, {"name": "art rock", "count": 4}],
                "tags": [{"name": "british", "count": 2}],
                "artist-credit": [{"artist": {"id": "artist-1", "name": "Queen"}}],
                "releases": [
                    {
                        "id": "release-early",
                        "title": "A Night at the Opera",
                        "status": "Official",
                        "date": "1975-11-21",
                        "country": "GB",
                        "packaging": "Gatefold Cover",
                        "media": [{"format": "12\" Vinyl", "track-count": 2}],
                    },
                    {
                        "id": "release-late",
                        "title": "A Night at the Opera",
                        "status": "Official",
                        "date": "1991",
                        "media": [{"format": "CD", "track-count": 2}],
                    },
                ],
            }
        if path == "/release/release-early":
            return {
                "media": [
                    {
                        "position": 1,
                        "track-count": 2,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "Death on Two Legs",
                                "length": 223733,
                                "recording": {"id": "recording-1", "title": "Death on Two Legs"},
                            },
                            {
                                "position": 2,
                                "title": "Lazing on a Sunday Afternoon",
                                "recording": {"id": "recording-2", "length": 68000},
                            },
                        ],
                    }
                ]
            }
        return None

    monkeypatch.setattr(MusicBrainzModule, "_request_json", staticmethod(fake_request))

    album = module.music_album("musicbrainz", "release-group-1")

    assert album is not None
    assert album.metadata_category == "Album / Live"
    assert album.category == ""
    assert album.rating == 8.5
    assert album.genres[:2] == ["rock", "art rock"]
    assert [track.media_id for track in album.tracks] == ["recording-1", "recording-2"]
    assert album.tracks[0].duration == 224
    assert album.tracks[0].track_number == 1
    assert album.tracks[0].disc_number == 1
    assert album.tracks[0].album_id == "release-group-1"
    assert album.tracks[1].title == "Lazing on a Sunday Afternoon"
    assert [release.media_id for release in album.releases] == ["release-early", "release-late"]
    assert album.releases[0].formats == ['12" Vinyl']
    assert album.track_count == 2


def test_music_artist_maps_profile_links_and_image(monkeypatch):
    """艺术家详情应整理活跃时间、别名、外链并把维基共享页转为图片直链。"""
    module = MusicBrainzModule()
    monkeypatch.setattr(
        MusicBrainzModule,
        "_request_json",
        staticmethod(
            lambda path, params=None: {
                "id": "artist-1",
                "name": "Queen",
                "sort-name": "Queen",
                "type": "Group",
                "disambiguation": "UK rock group",
                "country": "GB",
                "area": {"name": "United Kingdom"},
                "life-span": {"begin": "1970-06-27", "ended": True},
                "genres": [{"name": "rock", "count": 20}, {"name": "glam rock", "count": 9}],
                "tags": [{"name": "british", "count": 15}],
                "aliases": [{"name": "皇后乐队", "count": 0}],
                "relations": [
                    {
                        "type": "image",
                        "target-type": "url",
                        "url": {"resource": "https://commons.wikimedia.org/wiki/File:Queen.jpg"},
                    },
                    {
                        "type": "official homepage",
                        "target-type": "url",
                        "url": {"resource": "http://www.queenonline.com/"},
                    },
                    {
                        "type": "wikidata",
                        "target-type": "url",
                        "url": {"resource": "https://www.wikidata.org/wiki/Q15862"},
                    },
                    {
                        "type": "creative commons licensed download",
                        "target-type": "url",
                        "url": {"resource": "https://example.com/ignored"},
                    },
                ],
            }
        ),
    )

    artist = module.music_artist("musicbrainz", "artist-1")

    assert artist is not None
    assert artist.artist_type == "Group"
    assert artist.area == "United Kingdom"
    assert artist.life_span == "1970-06-27"
    assert artist.genres == ["rock", "glam rock"]
    assert artist.aliases == ["皇后乐队"]
    assert artist.image_url == (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Queen.jpg?width=500"
    )
    assert set(artist.external_links) == {"official homepage", "wikidata"}


def test_music_artist_albums_sorts_page_by_release_date(monkeypatch):
    """艺术家专辑列表应按发行日期倒序，并带上专辑类型筛选参数。"""
    module = MusicBrainzModule()
    requested = {}

    def fake_request(path, params=None):
        """记录浏览请求参数并返回两个乱序专辑。"""
        requested.update(path=path, params=params)
        return {
            "release-groups": [
                {
                    "id": "release-group-old",
                    "title": "Queen",
                    "primary-type": "Album",
                    "first-release-date": "1973-07-13",
                    "artist-credit": [{"artist": {"id": "artist-1", "name": "Queen"}}],
                },
                {
                    "id": "release-group-new",
                    "title": "News of the World",
                    "primary-type": "Album",
                    "first-release-date": "1977-10-28",
                    "artist-credit": [{"artist": {"id": "artist-1", "name": "Queen"}}],
                },
            ]
        }

    monkeypatch.setattr(MusicBrainzModule, "_request_json", staticmethod(fake_request))

    albums = module.music_artist_albums("musicbrainz", "artist-1", page=2, count=10, album_type="album")

    assert requested["path"] == "/release-group"
    assert requested["params"]["artist"] == "artist-1"
    assert requested["params"]["offset"] == 10
    assert requested["params"]["type"] == "album"
    assert [album.media_id for album in albums] == ["release-group-new", "release-group-old"]
    assert albums[0].music_type == "album"


def test_music_artist_related_prefers_meaningful_relations(monkeypatch):
    """关联艺术家应优先返回成员与子团体关系，致敬乐队排在最后。"""
    module = MusicBrainzModule()
    monkeypatch.setattr(
        MusicBrainzModule,
        "_request_json",
        staticmethod(
            lambda path, params=None: {
                "relations": [
                    {
                        "type": "tribute",
                        "target-type": "artist",
                        "artist": {"id": "artist-tribute", "name": "Queen Tribute"},
                    },
                    {
                        "type": "member of band",
                        "target-type": "artist",
                        "artist": {"id": "artist-member", "name": "Brian May"},
                    },
                    {
                        "type": "member of band",
                        "target-type": "artist",
                        "artist": {"id": "artist-member", "name": "Brian May"},
                    },
                    {
                        "type": "allmusic",
                        "target-type": "url",
                        "url": {"resource": "https://example.com"},
                    },
                ]
            }
        ),
    )

    related = module.music_artist_related("musicbrainz", "artist-1", count=5)

    assert [item.media_id for item in related] == ["artist-member", "artist-tribute"]
    assert related[0].relation == "member of band"
    assert related[0].music_type == "artist"


class _FakeMusicBrainzResponse:
    """模拟 MusicBrainz HTTP 响应，便于缓存回归测试统计网络调用次数。"""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        """返回预设的 JSON 负载。"""
        return self._payload

    def __bool__(self):
        """模拟 requests.Response：HTTP 错误状态在布尔判断中为 False。"""
        return self.status_code < 400

    def close(self):
        """无需释放的资源。"""


def test_request_json_caches_repeated_calls(monkeypatch):
    """相同路径与参数的 MusicBrainz 请求应命中缓存，避免重复发起网络调用。"""
    import app.modules.musicbrainz as musicbrainz_module

    monkeypatch.setattr(
        MusicBrainzModule, "_wait_for_rate_limit", classmethod(lambda cls: None)
    )
    network_calls = {"count": 0}

    def fake_get_res(_self, url, params=None):
        """记录网络调用次数并返回固定的录音详情。"""
        network_calls["count"] += 1
        return _FakeMusicBrainzResponse({"id": "recording-cache", "title": "晴天"})

    monkeypatch.setattr(musicbrainz_module.RequestUtils, "get_res", fake_get_res)
    # 清理缓存区，排除其他用例残留
    MusicBrainzModule._request_json.cache_clear()

    first = MusicBrainzModule._request_json("/recording/recording-cache", params={"fmt": "json"})
    second = MusicBrainzModule._request_json("/recording/recording-cache", params={"fmt": "json"})

    assert first == second
    assert network_calls["count"] == 1


def test_request_json_caches_not_found(monkeypatch):
    """MusicBrainz 稳定 404 应进入有界缓存，避免重复探测单曲与专辑入口。"""
    import app.modules.musicbrainz as musicbrainz_module

    monkeypatch.setattr(
        MusicBrainzModule, "_wait_for_rate_limit", classmethod(lambda cls: None)
    )
    network_calls = {"count": 0}

    def fake_get_res(_self, url, params=None):
        """始终返回 404，用于验证稳定不存在结果会被缓存。"""
        network_calls["count"] += 1
        return _FakeMusicBrainzResponse(None, status_code=404)

    monkeypatch.setattr(musicbrainz_module.RequestUtils, "get_res", fake_get_res)
    MusicBrainzModule._request_json.cache_clear()

    first = MusicBrainzModule._request_json("/recording/missing", params={"fmt": "json"})
    second = MusicBrainzModule._request_json("/recording/missing", params={"fmt": "json"})

    assert first == second == {}
    assert network_calls["count"] == 1


def test_request_json_retries_on_server_busy(monkeypatch):
    """服务端繁忙（429/5xx）属于瞬时错误，应退避重试而不是直接放弃。"""
    import time

    import app.modules.musicbrainz as musicbrainz_module

    monkeypatch.setattr(
        MusicBrainzModule, "_wait_for_rate_limit", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    responses = [
        _FakeMusicBrainzResponse(None, status_code=503),
        _FakeMusicBrainzResponse({"id": "recording-busy", "title": "晴天"}),
    ]

    def fake_get_res(_self, url, params=None):
        """依次返回繁忙与成功响应，验证重试后拿到结果。"""
        return responses.pop(0)

    monkeypatch.setattr(musicbrainz_module.RequestUtils, "get_res", fake_get_res)
    MusicBrainzModule._request_json.cache_clear()

    result = MusicBrainzModule._request_json("/recording/search-busy", params={"fmt": "json"})

    assert result == {"id": "recording-busy", "title": "晴天"}
    assert not responses


def test_same_text_normalizes_traditional_chinese():
    """候选比对应归一繁简差异，条目繁体写法不与简体资源标题失配。"""
    assert MusicBrainzModule._same_text("神的遊戲", "神的游戏")
    assert MusicBrainzModule._same_text("張懸", "张悬")
    assert not MusicBrainzModule._same_text("神的遊戲", "神的冒险")


def test_search_title_replaces_sanitized_underscores():
    """流媒体文件名消毒产生的下划线应转空格，不破坏检索短语。"""
    assert MusicBrainzModule._search_title("百年經典7_愛的奉獻") == "百年经典7 爱的奉献"


def test_select_candidate_matches_traditional_chinese_recording():
    """条目为繁体写法时，简体资源标题仍应命中候选。"""
    meta = MetaMusic(title="芸开了", artists=["许茹芸"])
    candidate = MusicInfo(
        media_source="musicbrainz",
        music_type="recording",
        media_id="recording-1",
        title="芸開了",
        artists=["許茹芸"],
    )

    matched = MusicBrainzModule._select_candidate(meta, [candidate], media_source="musicbrainz")

    assert matched is not None
    assert matched.media_id == "recording-1"


def test_recording_queries_ladder_relaxes_to_bare_title_last():
    """检索阶梯由严到宽放宽，裸标题兜底只能出现在最后一级。"""
    queries = MusicBrainzModule._recording_queries(
        MetaMusic(title="晴天 (电影版)", artists=["周杰伦"])
    )

    full = '("晴天 (电影版)" OR "晴天 (電影版)")'
    assert queries[0] == f'recording:{full} AND artist:("周杰伦" OR "周杰倫")'
    assert queries[1] == f'recording:{full}'
    assert queries[2] == 'recording:"晴天" AND artist:("周杰伦" OR "周杰倫")'
    # 全名查询之后才逐字兜底，避免单字噪声抢占召回窗口。
    assert queries[-1] == 'recording:("晴" OR "天")'


def test_query_phrase_prefers_complete_names_with_explicit_loose_fallback():
    """所有文字优先完整短语，CJK 只有显式末级兜底才拆为逐字 OR。"""
    assert MusicBrainzModule._query_phrase("Fearless") == '"Fearless"'
    assert MusicBrainzModule._query_phrase("晴天") == '"晴天"'
    assert MusicBrainzModule._query_phrase("好歌茹芸 Vol. 3") == '"好歌茹芸 Vol. 3"'
    assert MusicBrainzModule._query_phrase("好歌茹芸 Vol. 3", loose=True) == (
        '(("好" OR "歌" OR "茹" OR "芸") OR "Vol." OR "3")'
    )
    assert MusicBrainzModule._query_phrase("") is None


def test_same_text_normalizes_cjk_numerals():
    """汉字数字与阿拉伯数字写法差异不应阻断候选比对。"""
    assert MusicBrainzModule._same_text("茹此精彩十三首", "茹此精彩13首")
    assert MusicBrainzModule._same_text("二十周年演唱会", "20周年演唱会")
    assert not MusicBrainzModule._same_text("茹此精彩十三首", "茹此精彩14首")


def test_strip_artist_prefix_removes_signature_prefix():
    """曲名开头的艺术家署名前缀是命名习惯，检索与比对应使用主体名。"""
    assert MusicBrainzModule._strip_artist_prefix(
        "许茹芸的爱情电影主题曲", ["许茹芸"]) == "爱情电影主题曲"
    # 剥离后无剩余时保留原标题，短标题不受影响
    assert MusicBrainzModule._strip_artist_prefix("许茹芸", ["许茹芸"]) == "许茹芸"
    assert MusicBrainzModule._strip_artist_prefix("晴天", ["周杰伦"]) == "晴天"


def test_select_album_candidate_matches_lead_token_structure():
    """条目「主体名 补充说明」结构与资源主体名首段一致时应弱匹配命中。"""
    meta = MetaMusic(title="许茹芸的爱情电影主题曲", artists=["许茹芸"], year=2003)
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="愛情電影主題曲 雲且留住",
        artists=["許茹芸"],
        year=2003,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-1"


def test_select_album_candidate_matches_performance_suffix():
    """资源标题带演出后缀（S.H.E十七音乐会）时，条目本体一致应弱匹配命中。"""
    meta = MetaMusic(title="S.H.E十七音乐会", artists=["S.H.E"], year=2018)
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="十七",
        artists=["S.H.E"],
        year=2018,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-1"


def test_select_album_candidate_strips_volume_suffix():
    """系列专辑卷号后缀（Vol. 3）是发行分卷标记，本体名一致应弱匹配命中。"""
    meta = MetaMusic(title="好歌茹芸, Vol. 3", artists=["许茹芸"], year=2011)
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="好歌, 茹芸: Valen Hsu Greatest Hits",
        artists=["許茹芸"],
        year=2011,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-1"


def test_select_album_candidate_rejects_wrong_volume():
    """资源带卷号时其他分卷候选不能被弱匹配采信。"""
    meta = MetaMusic(title="Ibiza Lounge Moments, Vol. 1", artists=["Various Artists"], year=2022)
    wrong_volume = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-wrong",
        title="Ibiza Lounge Moments, Vol. 3",
        artists=["Various Artists"],
        year=2023,
    )

    assert MusicBrainzModule._select_album_candidate(meta, [wrong_volume]) is None


def test_select_album_candidate_matches_contained_title():
    """条目带额外前缀完整包含资源主体名（原声带类）时应弱匹配命中。"""
    meta = MetaMusic(
        title="Once Upon a Time in Hollywood Original Motion Picture Soundtrack",
        artists=["Various Artists"],
        year=2019,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="Quentin Tarantino's Once Upon a Time in Hollywood: "
              "Original Motion Picture Soundtrack",
        artists=["Various Artists"],
        year=2019,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-1"


def test_soundtrack_body_extracts_movie_name():
    """原声带描述词尾部剔除返回电影名本体，非原声带标题与词内后缀不受影响。"""
    body = MusicBrainzModule._soundtrack_body
    assert body("Pulp Fiction Original Motion Picture Soundtrack") == "Pulp Fiction"
    assert body("Reply 1988 OST") == "Reply 1988"
    # Ghost 尾部的 ost 是单词组成部分，不能剔除
    assert body("Ghost") == ""
    assert body("晴天") == ""


def test_search_title_strips_repeated_trailing_year():
    """场景命名重复携带的尾部年份应全部剥离，避免年份文本阻断精确匹配。"""
    assert MusicBrainzModule._search_title("Live At Montreux 2011 2011") == "Live At Montreux"
    # 纯年份标题无前导空白不受影响
    assert MusicBrainzModule._search_title("1999") == "1999"


def test_album_queries_include_soundtrack_body():
    """原声带专辑检索阶梯应包含电影名本体变体。"""
    queries = MusicBrainzModule._album_queries(
        MetaMusic(title="Pulp Fiction Original Motion Picture Soundtrack",
                  artists=["Various Artists"], year=1994)
    )

    assert 'releasegroup:"Pulp Fiction" AND artist:"Various Artists"' in queries


def test_select_album_candidate_matches_soundtrack_body():
    """条目以冒号副标题保留描述词时，电影名本体一致应弱匹配命中。"""
    meta = MetaMusic(
        title="Pulp Fiction Original Motion Picture Soundtrack",
        artists=["Various Artists"],
        year=1994,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="Pulp Fiction: Music From the Motion Picture",
        artists=["Various Artists"],
        year=1994,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-1"


def test_select_candidate_rejects_wrong_artist_same_title():
    """已知艺术家时，同名异曲的候选不能因标题相等被采信。"""
    meta = MetaMusic(title="因为有你", artists=["毛阿敏"])
    wrong_artist = MusicInfo(
        media_source="musicbrainz",
        music_type="recording",
        media_id="recording-wrong",
        title="因为有你",
        artists=["张蔷"],
    )

    assert MusicBrainzModule._select_candidate(meta, [wrong_artist], media_source="musicbrainz") is None


def test_select_candidate_rejects_artist_only_match():
    """CJK 逐字 OR 检索召回宽，标题未命中的候选不能仅凭艺术家署名被采信。"""
    meta = MetaMusic(title="茹此精彩十三首", artists=["许茹芸"])
    same_artist_other_song = MusicInfo(
        media_source="musicbrainz",
        music_type="recording",
        media_id="recording-wrong",
        title="半首歌",
        artists=["许茹芸"],
    )

    assert MusicBrainzModule._select_candidate(
        meta, [same_artist_other_song], media_source="musicbrainz") is None


def test_select_album_candidate_requires_title_and_artist():
    """专辑候选需标题（含去括号弱匹配）与艺术家同时命中才采信。"""
    meta = MetaMusic(title="我爱夜 (新歌+精选)", artists=["许茹芸"], year=2003)
    album_hit = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-1",
        title="我爱夜",
        artists=["许茹芸"],
        year=2003,
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album_hit])

    assert matched is not None
    assert matched.media_id == "album-1"

    album_wrong_artist = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-2",
        title="我爱夜",
        artists=["其他歌手"],
    )

    assert MusicBrainzModule._select_album_candidate(meta, [album_wrong_artist]) is None


def test_select_album_candidate_matches_colon_subtitle():
    """条目「主标题：副标题」结构应与资源主标题弱匹配命中。"""
    meta = MetaMusic(title="天国的情人", artists=["邓丽君"])
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-colon",
        title="天國的情人：鄧麗君逝世十周年紀念聲影存集",
        artists=["鄧麗君"],
    )

    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-colon"


def test_select_album_candidate_matches_head_title():
    """说明性专辑标题可弱匹配，但仍须具有相同现场版本证据。"""
    meta = MetaMusic(title="为你盛开", artists=["许巍"])
    album = MusicInfo(
        media_source="musicbrainz",
        music_type="album",
        media_id="album-head",
        title="为你盛开-许巍《无尽光芒》巡回演唱会现场纪念",
        artists=["许巍"],
    )

    assert MusicBrainzModule._select_album_candidate(meta, [album]) is None
    meta.version = "现场"
    matched = MusicBrainzModule._select_album_candidate(meta, [album])

    assert matched is not None
    assert matched.media_id == "album-head"


def test_search_title_strips_trailing_year():
    """检索式应剥离曲名尾部独立年份，避免年份文本造成精确短语零命中。"""
    assert MusicBrainzModule._search_title("Funky Jazz Saxophone 2024") == "Funky Jazz Saxophone"
    # 非尾部年份属于曲名内容，不应剥离
    assert MusicBrainzModule._search_title("2002年的第一场雪") == "2002年的第一场雪"
