"""内置媒体来源投影与分类能力声明的一致性测试。"""

from collections.abc import Iterable

from app.domain.classification.facts import build_classification_facts
from app.domain.classification.sources import (
    FIXTURE_CLASSIFICATION_SOURCES,
    builtin_source_field_support,
)
from app.domain.context import MediaInfo
from app.modules.douban import DoubanModule
from app.modules.imdb import ImdbModule
from app.modules.imdb.api import ImdbTitle
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.modules.thetvdb import TheTvDbModule
from app.schemas.category import ClassificationFacts
from app.schemas.types import MediaSource, MediaType


def _facts(items: Iterable[object]) -> tuple[ClassificationFacts, ...]:
    """把来源投影对象批量转换为分类事实快照。"""
    return tuple(build_classification_facts(item) for item in items)


def _fact_value(facts: ClassificationFacts, field_id: str) -> object:
    """按字段目录 ID 读取强类型事实值。"""
    section_name, field_name = field_id.split(".", 1)
    section = getattr(facts, section_name)
    return getattr(section, field_name) if section is not None else None


def _tmdb_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造 TMDB 详情与摘要投影。"""
    return _facts(
        [
            MediaInfo(
                tmdb_info={
                    "id": 101,
                    "media_type": "tv",
                    "name": "示例动画",
                    "first_air_date": "2024-01-01",
                    "original_language": "ja",
                    "origin_country": ["JP"],
                    "genre_ids": [16, 18],
                    "genres": [
                        {"id": 16, "name": "Animation"},
                        {"id": 18, "name": "Drama"},
                    ],
                    "adult": False,
                    "episode_run_time": [24],
                    "content_rating": "TV-14",
                    "production_companies": [{"name": "Studio A"}],
                    "networks": [{"name": "Network A"}],
                }
            ),
            MediaInfo(
                tmdb_info={
                    "id": 102,
                    "media_type": "tv",
                    "name": "摘要条目",
                    "first_air_date": "2024",
                }
            ),
        ]
    )


def _douban_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造豆瓣影视详情投影。"""
    return _facts(
        [
            MediaInfo(
                douban_info={
                    "id": "201",
                    "title": "示例电影",
                    "subtype": "movie",
                    "year": "2023",
                    "genres": ["剧情", "动画"],
                    "countries": ["中国大陆"],
                    "durations": ["108分钟"],
                }
            ),
            MediaInfo(
                douban_info={
                    "id": "202",
                    "title": "摘要电影",
                    "target_type": "movie",
                    "year": "2024",
                }
            ),
        ]
    )


def _bangumi_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造 Bangumi 详情投影。"""
    return _facts(
        [
            MediaInfo(
                bangumi_info={
                    "id": 301,
                    "name_cn": "示例番剧",
                    "platform": "TV",
                    "date": "2022-10-01",
                    "tags": [{"name": "动画"}, {"name": "奇幻"}],
                    "infobox": [
                        {"key": "动画制作", "value": [{"v": "Studio B"}]}
                    ],
                }
            ),
            MediaInfo(
                bangumi_info={
                    "id": 302,
                    "name": "Summary Anime",
                }
            ),
        ]
    )


def _anilist_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造 AniList 详情投影。"""
    return _facts(
        [
            MediaInfo(
                anilist_info={
                    "id": 401,
                    "title": {"native": "示例动画"},
                    "format": "TV",
                    "startDate": {"year": 2021, "month": 4, "day": 1},
                    "countryOfOrigin": "JP",
                    "genres": ["Fantasy", "Drama"],
                    "isAdult": False,
                    "duration": 25,
                    "studios": {"nodes": [{"name": "Studio C"}]},
                }
            ),
            MediaInfo(
                anilist_info={
                    "id": 402,
                    "title": {"native": "示例国创"},
                    "format": "ONA",
                    "startDate": {"year": 2022},
                    "countryOfOrigin": "CN",
                    "genres": ["Drama"],
                    "isAdult": False,
                    "duration": 20,
                    "studios": {"nodes": [{"name": "Studio D"}]},
                }
            ),
        ]
    )


def _imdb_fixtures() -> tuple[ClassificationFacts, ...]:
    """通过 IMDb Module 的统一投影构造详情事实。"""
    media = ImdbModule._to_media_info(
        ImdbTitle(
            id="tt0000501",
            type="movie",
            primaryTitle="Example Movie",
            startYear=2020,
            runtimeSeconds=7200,
            genres=["Crime", "Drama"],
            originCountries=[{"code": "US", "name": "United States"}],
            spokenLanguages=[{"code": "eng", "name": "English"}],
            isAdult=False,
        )
    )
    summary = ImdbModule._to_media_info(
        ImdbTitle(
            id="tt0000502",
            type="movie",
            primaryTitle="Summary Movie",
            startYear=2021,
        )
    )
    return _facts([media, summary])


def _tvdb_fixtures() -> tuple[ClassificationFacts, ...]:
    """通过 TVDB 候选解析构造带年份和缺年份的标准投影。"""
    module = object.__new__(TheTvDbModule)
    base = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="base",
        type=MediaType.TV,
        title="示例剧",
    )
    with_year = module._resolve_auxiliary_candidates(
        base,
        [{"id": 501, "name": "示例剧", "year": "2025"}],
    )[0]
    without_year = module._resolve_auxiliary_candidates(
        base,
        [{"id": 502, "name": "示例剧"}],
    )[0]
    return _facts([with_year, without_year])


def _musicbrainz_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造 MusicBrainz 单曲、专辑、艺术家和最小摘要投影。"""
    recording = MusicBrainzModule._recording_to_info(
        {
            "id": "recording-1",
            "title": "Live Song",
            "genres": [{"name": "Rock", "count": 3}],
            "first-release-date": "2024-01-02",
            "releases": [
                {
                    "title": "Live Album",
                    "status": "Official",
                    "release-group": {
                        "id": "album-1",
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
                }
            ],
        }
    )
    album = MusicBrainzModule._release_group_to_album(
        {
            "id": "album-1",
            "title": "Live Album",
            "primary-type": "Album",
            "secondary-types": ["Live"],
            "first-release-date": "2024-01-02",
            "genres": [{"name": "Rock", "count": 4}],
            "tags": [{"name": "concert", "count": 2}],
        }
    )
    artist = MusicBrainzModule._artist_to_info(
        {
            "id": "artist-1",
            "name": "Example Artist",
            "country": "GB",
            "genres": [{"name": "Rock", "count": 4}],
            "tags": [{"name": "british", "count": 2}],
        }
    )
    minimal = MusicBrainzModule._recording_to_info(
        {"id": "recording-2", "title": "Summary Song"}
    )
    assert recording and album and artist and minimal
    return _facts([recording, album, artist, minimal])


def _theaudiodb_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造 TheAudioDB 单曲、专辑、艺术家和最小摘要投影。"""
    track = TheAudioDbModule._track_to_info(
        {
            "idTrack": "track-1",
            "strTrack": "Example Track",
            "strGenre": "Rock",
            "strStyle": "Pop",
        }
    )
    album = TheAudioDbModule._album_to_info(
        {
            "idAlbum": "album-1",
            "strAlbum": "Example Album",
            "strReleaseDate": "2023-04-05",
            "strReleaseFormat": "Album",
            "strGenre": "Rock",
            "strStyle": "Pop",
            "strMood": "Energetic",
        }
    )
    artist = TheAudioDbModule._artist_to_info(
        {
            "idArtist": "artist-1",
            "strArtist": "Example Artist",
            "strCountry": "United Kingdom",
            "strGenre": "Rock",
        }
    )
    minimal = TheAudioDbModule._track_to_info(
        {"idTrack": "track-2", "strTrack": "Summary Track"}
    )
    assert track and minimal
    return _facts([track, album, artist, minimal])


def _douban_music_fixtures() -> tuple[ClassificationFacts, ...]:
    """构造豆瓣音乐搜索摘要、专辑详情和曲目投影。"""
    summary = DoubanModule._build_music_search_results(
        {"items": [{"id": "album-2", "type": "music", "title": "摘要专辑"}]}
    )[0]
    album = DoubanModule._douban_music_to_album(
        {
            "id": "album-1",
            "title": "示例专辑",
            "pubdate": ["2022-06-01"],
            "media": ["CD"],
            "genres": ["摇滚"],
            "tags": [{"name": "现场"}],
            "songs": [{"title": "示例歌曲", "track_number": 1}],
        }
    )
    assert album and album.tracks
    return _facts([summary, album, album.tracks[0]])


_SOURCE_FIXTURE_BUILDERS = {
    MediaSource.TMDB.value: _tmdb_fixtures,
    MediaSource.Douban.value: _douban_fixtures,
    MediaSource.Bangumi.value: _bangumi_fixtures,
    MediaSource.AniList.value: _anilist_fixtures,
    MediaSource.IMDb.value: _imdb_fixtures,
    MediaSource.TVDB.value: _tvdb_fixtures,
    MediaSource.MusicBrainz.value: _musicbrainz_fixtures,
    MediaSource.TheAudioDB.value: _theaudiodb_fixtures,
    MediaSource.DoubanMusic.value: _douban_music_fixtures,
}


def test_source_capability_declarations_match_real_projection_fixtures() -> None:
    """可用能力必须有真实正例，部分能力还必须保留缺失样本。"""
    for media_source in FIXTURE_CLASSIFICATION_SOURCES:
        fixtures = _SOURCE_FIXTURE_BUILDERS[media_source]()
        support = builtin_source_field_support(media_source)

        for field_id, level in support.items():
            availability = [_fact_value(facts, field_id) is not None for facts in fixtures]
            if level in {"native", "derived"}:
                assert any(availability), f"{media_source} 的 {field_id} 缺少正例"
            elif level == "partial":
                assert any(availability), f"{media_source} 的 {field_id} 缺少部分支持正例"
                assert not all(availability), f"{media_source} 的 {field_id} 应提供缺失样本"


def test_unavailable_source_fields_remain_missing_in_real_fixtures() -> None:
    """声明不可用的字段不能因领域默认值而伪装成来源事实。"""
    for media_source in FIXTURE_CLASSIFICATION_SOURCES:
        fixtures = _SOURCE_FIXTURE_BUILDERS[media_source]()
        support = builtin_source_field_support(media_source)

        for field_id, level in support.items():
            if level != "unavailable":
                continue
            assert all(
                _fact_value(facts, field_id) is None for facts in fixtures
            ), f"{media_source} 的 {field_id} 声明不可用但 fixture 存在值"


def test_source_fixtures_cover_every_builtin_catalog_source() -> None:
    """能力目录新增内置来源时必须同步提供真实投影 fixture。"""
    assert tuple(_SOURCE_FIXTURE_BUILDERS) == FIXTURE_CLASSIFICATION_SOURCES


def test_country_and_genre_normalization_produce_cross_source_keys() -> None:
    """来源名称差异必须收敛为稳定国家代码和类型键。"""
    douban = _douban_fixtures()[0]
    theaudiodb = _theaudiodb_fixtures()

    assert douban.media.countries == ["CN"]
    assert douban.media.genre_keys == ["drama", "animation"]
    assert any(item.media.countries == ["GB"] for item in theaudiodb)
    assert any(item.media.genre_keys == ["rock", "pop"] for item in theaudiodb)


def test_unknown_genre_name_does_not_create_unstable_key() -> None:
    """未知来源类型只保留显示名，不能自动制造不稳定分类键。"""
    facts = build_classification_facts(
        MediaInfo(
            media_source=MediaSource.Douban,
            media_id="unknown-genre",
            type=MediaType.MOVIE,
            title="未知类型电影",
            genres=[{"name": "尚未收录的类型"}],
        )
    )

    assert facts.media.genre_names == ["尚未收录的类型"]
    assert facts.media.genre_keys is None
