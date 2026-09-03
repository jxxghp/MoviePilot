"""TMDB 详情到统一媒体字段的纯投影规则。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional

from app.domain.projection.mapping import ProjectionBuilder
from app.schemas.types import MediaSource, MediaType


def _identity_image_url(path: str) -> Optional[str]:
    """在启动组合根尚未注入图片策略时保留来源路径。"""
    return path


_image_url_builder: Callable[[str], Optional[str]] = _identity_image_url


def configure_image_url_builder(
    builder: Callable[[str], Optional[str]],
) -> None:
    """注入 TMDB 图片地址构造器，投影规则不直接读取平台配置。"""
    global _image_url_builder
    _image_url_builder = builder


def _media_type(info: Mapping[str, Any]) -> MediaType:
    """按 TMDB 详情形态解析标准媒体类型。"""
    source_type = info.get("media_type")
    if isinstance(source_type, MediaType):
        return source_type
    if source_type:
        return MediaType.MOVIE if source_type == "movie" else MediaType.TV
    return MediaType.MOVIE if info.get("title") else MediaType.TV


def _credits(info: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 TMDB credits 中按历史部门规则提取导演和演员。"""
    credits = info.get("credits")
    if not isinstance(credits, Mapping):
        return [], []
    actors = [
        dict(cast)
        for cast in credits.get("cast") or []
        if isinstance(cast, Mapping) and cast.get("known_for_department") == "Acting"
    ]
    directors = [
        dict(crew)
        for crew in credits.get("crew") or []
        if isinstance(crew, Mapping) and crew.get("job") in {"Director", "Writer", "Editor", "Producer"}
    ]
    return directors, actors


def _release_dates(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 TMDB 分地区上映日期压平为统一发行日期列表。"""
    release_dates = info.get("release_dates")
    if not isinstance(release_dates, Mapping):
        return []
    values: list[dict[str, Any]] = []
    for result in release_dates.get("results") or []:
        if not isinstance(result, Mapping):
            continue
        for release in result.get("release_dates") or []:
            if not isinstance(release, Mapping) or not release.get("release_date"):
                continue
            values.append(
                {
                    "date": release.get("release_date"),
                    "iso_code": result.get("iso_3166_1"),
                    "note": release.get("note"),
                    "type": release.get("type"),
                }
            )
    return values


def _project_seasons(
    builder: ProjectionBuilder,
    seasons: object,
) -> None:
    """投影 TMDB 电视剧季集清单和季首播年份。"""
    if (
        not seasons
        or not isinstance(seasons, Sequence)
        or isinstance(seasons, (str, bytes))
    ):
        return
    season_info: list[dict[str, Any]] = []
    episode_map: dict[int, list[int]] = {}
    year_map: dict[int, str] = {}
    for item in seasons:
        if not isinstance(item, Mapping):
            continue
        season = item.get("season_number")
        if not isinstance(season, int):
            continue
        season_info.append(dict(item))
        episode_count = item.get("episode_count")
        if isinstance(episode_count, int) and episode_count >= 0:
            episode_map[season] = list(range(1, episode_count + 1))
        air_date = item.get("air_date")
        if air_date:
            year_map[season] = str(air_date)[:4]
    builder.set("season_info", season_info)
    builder.set("seasons", episode_map)
    builder.set("season_years", year_map)


def project(
    current: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """将只读 TMDB 详情投影为统一媒体字段，不修改输入。"""
    if not info:
        return {}
    builder = ProjectionBuilder(current)
    media_type = _media_type(info)
    media_id = str(info.get("id")) if info.get("id") is not None else None
    builder.set("media_source", MediaSource.TMDB)
    builder.set("tmdb_info", info)
    builder.set("type", media_type)
    builder.set("media_id", media_id)
    builder.set("tmdb_id", info.get("id"))
    if not media_id:
        return builder.build()

    external_ids = info.get("external_ids")
    if isinstance(external_ids, Mapping):
        builder.set("tvdb_id", external_ids.get("tvdb_id"))
        builder.set("imdb_id", external_ids.get("imdb_id"))
    builder.set("collection_id", info.get("collection_id"))
    vote_average = info.get("vote_average")
    builder.set("vote_average", round(float(vote_average), 1) if vote_average else 0)
    builder.set("overview", info.get("overview"))
    builder.set("genre_ids", info.get("genre_ids") or [])
    builder.set("adult", info.get("adult"))
    for name in ("original_language", "en_title", "hk_title", "tw_title", "sg_title"):
        builder.set(name, info.get(name))

    if media_type == MediaType.MOVIE:
        builder.set("title", info.get("title"))
        builder.set("original_title", info.get("original_title"))
        release_date = info.get("release_date")
        builder.set("release_date", release_date)
        if release_date:
            builder.set("year", str(release_date)[:4])
        builder.set("release_dates", _release_dates(info))
    else:
        builder.set("title", info.get("name"))
        builder.set("original_title", info.get("original_name"))
        release_date = info.get("first_air_date")
        builder.set("release_date", release_date)
        if release_date:
            builder.set("year", str(release_date)[:4])
        _project_seasons(builder, info.get("seasons"))
        episode_groups = info.get("episode_groups")
        if isinstance(episode_groups, Mapping):
            builder.set("episode_groups", episode_groups.get("results") or [])

    poster_path = info.get("poster_path")
    if poster_path:
        builder.set("poster_path", _image_url_builder(str(poster_path)))
    backdrop_path = info.get("backdrop_path")
    if backdrop_path:
        builder.set("backdrop_path", _image_url_builder(str(backdrop_path)))
    directors, actors = _credits(info)
    builder.set("directors", directors)
    builder.set("actors", actors)
    builder.set("names", info.get("names") or [])
    builder.fill_missing(info)
    return builder.build()
