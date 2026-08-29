"""统一媒体信息的外部来源投影合同。"""

from copy import deepcopy
from dataclasses import fields
from types import MappingProxyType

import pytest

from app.domain.context import MediaInfo
from app.domain.projection import anilist as anilist_projection
from app.domain.projection import bangumi as bangumi_projection
from app.domain.projection import douban as douban_projection
from app.domain.projection import tmdb as tmdb_projection
from app.schemas.context import MediaInfo as MediaInfoSchema
from app.schemas.types import MediaSource, MediaType


def test_tmdb_projection_is_complete_and_does_not_mutate_source(monkeypatch) -> None:
    """TMDB 电视剧详情应完整投影，且保留调用方原始 episode_groups。"""
    source = {
        "id": 100,
        "media_type": "tv",
        "name": "示例剧",
        "original_name": "Example Show",
        "first_air_date": "2024-01-02",
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "vote_average": 8.26,
        "external_ids": {"tvdb_id": 200, "imdb_id": "tt100"},
        "seasons": [
            {"season_number": 0, "episode_count": 2, "air_date": "2023-12-01"},
            {"season_number": 1, "episode_count": 3, "air_date": "2024-01-02"},
        ],
        "episode_groups": {"results": [{"id": "group-1"}]},
        "credits": {
            "cast": [
                {"id": 1, "name": "演员", "known_for_department": "Acting"},
                {"id": 2, "name": "制片", "known_for_department": "Production"},
            ],
            "crew": [
                {"id": 3, "name": "导演", "job": "Director"},
                {"id": 4, "name": "灯光", "job": "Lighting"},
            ],
        },
        "names": ["示例别名"],
    }
    original = deepcopy(source)
    monkeypatch.setattr(
        tmdb_projection,
        "_image_url_builder",
        lambda path: f"https://image.example{path}",
    )

    media = MediaInfo(tmdb_info=source)

    assert source == original
    assert media.tmdb_info == original
    assert media.media_source == MediaSource.TMDB
    assert media.media_id == "100"
    assert media.type == MediaType.TV
    assert media.title == "示例剧"
    assert media.original_title == "Example Show"
    assert media.release_date == "2024-01-02"
    assert media.year == "2024"
    assert media.tvdb_id == 200
    assert media.imdb_id == "tt100"
    assert media.vote_average == 8.3
    assert media.seasons == {0: [1, 2], 1: [1, 2, 3]}
    assert media.season_years == {0: "2023", 1: "2024"}
    assert media.episode_groups == [{"id": "group-1"}]
    assert media.poster_path == "https://image.example/poster.jpg"
    assert media.backdrop_path == "https://image.example/backdrop.jpg"
    assert media.actors == [
        {"id": 1, "name": "演员", "known_for_department": "Acting"}
    ]
    assert media.directors == [{"id": 3, "name": "导演", "job": "Director"}]
    assert media.names == ["示例别名"]


def test_tmdb_empty_seasons_preserve_existing_season_projection() -> None:
    """TMDB 空季列表应保持历史 skip 语义，不清空已有季集字段。"""
    season_info = [{"season_number": 1, "episode_count": 2}]
    media = MediaInfo(
        season_info=season_info,
        seasons={1: [1, 2]},
        season_years={1: "2024"},
        tmdb_info={
            "id": 101,
            "media_type": "tv",
            "name": "示例剧",
            "seasons": [],
        },
    )

    assert media.season_info == season_info
    assert media.seasons == {1: [1, 2]}
    assert media.season_years == {1: "2024"}


def test_douban_projection_is_complete_and_does_not_mutate_source() -> None:
    """豆瓣详情应统一处理季、年份、海报、别名、时长和国家字段。"""
    source = {
        "id": "1292052",
        "title": "示例剧 第二季",
        "original_title": "Example Show",
        "subtype": "tv",
        "year": "2025",
        "rating": {"value": "8.8"},
        "pubdate": ["中国大陆 2025-02-03"],
        "cover_url": "https://img.example/imageView2/0/q/80/w/9999/h/120/format/webp",
        "intro": "示例简介",
        "directors": [{"name": "导演"}],
        "actors": [{"name": "演员"}],
        "aka": ["示例别名(港台译名)"],
        "episodes_count": 4,
        "season_years": {1: None, 2: 2025},
        "genres": ["剧情"],
        "durations": ["45分钟"],
        "countries": ["中国大陆"],
    }
    original = deepcopy(source)

    media = MediaInfo(douban_info=source)

    assert source == original
    assert media.douban_info == original
    assert media.media_source == MediaSource.Douban
    assert media.media_id == "1292052"
    assert media.type == MediaType.TV
    assert media.title == "示例剧 第二季"
    assert media.season == 2
    assert media.year == "2025"
    assert media.vote_average == 8.8
    assert media.release_date == "2025-02-03"
    assert media.poster_path == "https://img.example/imageView2/1/w/500/h/750/format/webp"
    assert media.names == ["示例别名"]
    assert media.seasons == {2: [1, 2, 3, 4]}
    assert media.season_years == {2: "2025"}
    assert media.genres == [{"id": "剧情", "name": "剧情"}]
    assert media.runtime == 45
    assert media.production_countries == [{"id": "中国大陆", "name": "中国大陆"}]


def test_bangumi_projection_is_complete_and_does_not_mutate_source() -> None:
    """Bangumi 详情应统一处理剧场版、别名、制作和演职员字段。"""
    source = {
        "id": 300,
        "name": "Example Movie",
        "name_cn": "示例电影",
        "platform": "剧场版",
        "date": "2026-01-02",
        "rating": {"score": "9.1"},
        "images": {"large": "https://img.example/bangumi.jpg"},
        "summary": "Bangumi 简介",
        "tags": [{"name": "动画"}],
        "infobox": [
            {"key": "别名", "value": [{"v": "别名一"}, "别名二"]},
            {"key": "动画制作", "value": [{"v": "Studio A"}]},
            {"key": "导演", "value": "导演甲"},
        ],
        "actors": [{"name": "演员甲"}],
    }
    original = deepcopy(source)

    media = MediaInfo(bangumi_info=source)

    assert source == original
    assert media.bangumi_info == original
    assert media.media_source == MediaSource.Bangumi
    assert media.media_id == "300"
    assert media.type == MediaType.MOVIE
    assert media.title == "示例电影"
    assert media.original_title == "Example Movie"
    assert media.year == "2026"
    assert media.vote_average == 9.1
    assert media.poster_path == "https://img.example/bangumi.jpg"
    assert media.names == ["别名一", "别名二"]
    assert media.genres == [{"id": "动画", "name": "动画"}]
    assert media.production_companies == [{"name": "Studio A"}]
    assert media.directors == [{"name": "导演甲"}]
    assert media.actors == [{"name": "演员甲"}]


def test_anilist_projection_is_complete_and_does_not_mutate_source() -> None:
    """AniList 详情应统一处理中文标题、模糊日期、季集和 AniDB 身份。"""
    source = {
        "id": 400,
        "title": {
            "chinese": "葬送的芙莉莲",
            "native": "葬送のフリーレン",
            "romaji": "Sousou no Frieren",
            "english": "Frieren",
        },
        "format": "TV",
        "startDate": {"year": 2023, "month": 9, "day": 29},
        "endDate": {"year": 2024, "month": 3},
        "episodes": 3,
        "duration": 24,
        "averageScore": 91,
        "countryOfOrigin": "JP",
        "coverImage": {"extraLarge": "https://img.example/anilist.jpg"},
        "bannerImage": "https://img.example/banner.jpg",
        "description": "A <b>journey</b><br>afterwards",
        "genres": ["Fantasy"],
        "studios": {"nodes": [{"name": "Madhouse"}]},
        "actors": [{"name": "演员"}],
        "directors": [{"name": "导演"}],
        "externalLinks": [{"site": "AniDB", "url": "https://anidb.net/anime/17617"}],
    }
    original = deepcopy(source)

    media = MediaInfo(anilist_info=source)

    assert source == original
    assert media.anilist_info == original
    assert media.media_source == MediaSource.AniList
    assert media.media_id == "400"
    assert media.type == MediaType.TV
    assert media.title == "葬送的芙莉莲"
    assert media.en_title == "Frieren"
    assert media.original_title == "葬送のフリーレン"
    assert media.release_date == "2023-09-29"
    assert media.last_air_date == "2024-03"
    assert media.year == "2023"
    assert media.poster_path == "https://img.example/anilist.jpg"
    assert media.backdrop_path == "https://img.example/banner.jpg"
    assert media.overview == "A journey\nafterwards"
    assert media.vote_average == 9.1
    assert media.original_language == "ja"
    assert media.origin_country == ["JP"]
    assert media.production_companies == [{"name": "Madhouse"}]
    assert media.genres == [{"id": "Fantasy", "name": "Fantasy"}]
    assert media.seasons == {1: [1, 2, 3]}
    assert media.season_years == {1: "2023"}
    assert media.anidb_id == 17617


def test_multiple_source_projection_keeps_constructor_order_and_identity() -> None:
    """多来源详情同时存在时保持 TMDB 到 AniList 的历史应用顺序。"""
    media = MediaInfo(
        tmdb_info={"id": 1, "media_type": "movie", "title": "TMDB 标题"},
        douban_info={"id": "2", "title": "豆瓣标题", "type": "movie"},
        bangumi_info={"id": 3, "name_cn": "Bangumi 标题", "platform": "TV"},
        anilist_info={"id": 4, "title": {"chinese": "AniList 标题"}, "format": "TV"},
    )

    assert media.media_source == MediaSource.AniList
    assert media.media_id == "4"
    assert media.type == MediaType.MOVIE
    assert media.title == "TMDB 标题"
    assert media.tmdb_info["id"] == 1
    assert media.douban_info["id"] == "2"
    assert media.bangumi_info["id"] == 3
    assert media.anilist_info["id"] == 4


@pytest.mark.parametrize(
    ("projector", "source"),
    [
        (tmdb_projection.project, {"id": 1, "media_type": "movie", "title": "TMDB"}),
        (douban_projection.project, {"id": 2, "title": "豆瓣", "type": "movie"}),
        (bangumi_projection.project, {"id": 3, "name_cn": "Bangumi", "platform": "TV"}),
        (anilist_projection.project, {"id": 4, "title": {"chinese": "AniList"}, "format": "TV"}),
    ],
)
def test_projection_accepts_read_only_mappings_without_mutating_current_snapshot(
    projector, source: dict
) -> None:
    """四个 owner 都应接受只读 Mapping，并保持当前快照和来源输入不变。"""
    current = vars(MediaInfo())
    current_before = deepcopy(current)
    source_before = deepcopy(source)

    projected = projector(MappingProxyType(current), MappingProxyType(source))

    assert isinstance(projected, dict)
    assert current == current_before
    assert source == source_before


def test_media_schema_is_transport_projection_not_duplicate_domain_logic() -> None:
    """API DTO 只比领域数据字段多两个派生输出字段。"""
    domain_fields = {item.name for item in fields(MediaInfo)}
    schema_fields = set(MediaInfoSchema.model_fields)

    assert schema_fields - domain_fields == {"detail_link", "title_year"}
    assert domain_fields - schema_fields == set()


@pytest.mark.parametrize(
    ("method_name", "expected_parameters"),
    [
        ("set_tmdb_info", ("self", "info")),
        ("set_douban_info", ("self", "info")),
        ("set_bangumi_info", ("self", "info")),
        ("set_anilist_info", ("self", "info")),
    ],
)
def test_media_source_setter_abi_is_stable(
    method_name: str, expected_parameters: tuple[str, ...]
) -> None:
    """四个历史 setter 继续由 canonical MediaInfo 以原参数名提供。"""
    import inspect

    method = getattr(MediaInfo, method_name)
    assert tuple(inspect.signature(method).parameters) == expected_parameters
