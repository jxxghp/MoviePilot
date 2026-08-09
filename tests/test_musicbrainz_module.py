from app.core.meta import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule
from app.core.config import settings


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
    assert requested[1][1]["query"] == 'releasegroup:"晴天" AND artist:"周杰伦"'
    assert requested[2][1]["query"] == 'artist:"周杰伦"'


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
    """单曲 ID 不存在时应按专辑再识别一次，保证专辑订阅可恢复目标。"""
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
    assert album.category == "Album / Live"
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
