"""Bangumi 详情到统一媒体字段的纯投影规则。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.metainfo import MetaInfo
from app.domain.projection.mapping import ProjectionBuilder
from app.schemas.types import MediaSource, MediaType

MOVIE_PLATFORMS = frozenset({"movie", "电影", "剧场版"})


def resolve_media_type(info: Mapping[str, Any]) -> MediaType:
    """根据 Bangumi 媒介平台返回标准媒体类型。"""
    platform = str(info.get("platform") or "").strip().casefold()
    return MediaType.MOVIE if platform in MOVIE_PLATFORMS else MediaType.TV


def _aliases(info: Mapping[str, Any]) -> list[Any]:
    """从 Bangumi infobox 中读取稳定顺序的别名。"""
    infobox = info.get("infobox")
    if not isinstance(infobox, list):
        return []
    values = [item.get("value") for item in infobox if isinstance(item, Mapping) and item.get("key") == "别名"]
    if not values:
        return []
    first = values[0]
    if isinstance(first, list):
        return [item.get("v") if isinstance(item, Mapping) else item for item in first]
    return [first] if isinstance(first, str) else []


def _companies_directors(info: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 Bangumi infobox 中提取制作公司和导演。"""
    companies: list[dict[str, Any]] = []
    directors: list[dict[str, Any]] = []
    infobox = info.get("infobox")
    if not isinstance(infobox, list):
        return companies, directors
    for item in infobox:
        if not isinstance(item, Mapping):
            continue
        values = item.get("value")
        source_values = values if isinstance(values, list) else [values]
        normalized = [value.get("v") if isinstance(value, Mapping) else value for value in source_values if value]
        if item.get("key") in {"动画制作", "制作"}:
            companies.extend({"name": value} for value in normalized)
        elif item.get("key") == "导演":
            directors.extend({"name": value} for value in normalized)
    return companies, directors


def project(
    current: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """将只读 Bangumi 详情投影为统一媒体字段，不修改输入。"""
    if not info:
        return {}
    builder = ProjectionBuilder(current)
    media_id = str(info.get("id")) if info.get("id") is not None else None
    builder.set("media_source", MediaSource.Bangumi)
    builder.set("bangumi_info", info)
    builder.set("media_id", media_id)
    builder.set("bangumi_id", info.get("id"))
    if not builder.get("type"):
        builder.set("type", resolve_media_type(info))
    builder.set_missing("title", info.get("name_cn") or info.get("name"))
    builder.set_missing("original_title", info.get("name"))
    meta = MetaInfo(str(builder.get("title") or ""))
    if builder.get("season") is None:
        builder.set("season", meta.begin_season)
    if not builder.get("vote_average"):
        rating = info.get("rating")
        score = rating.get("score") if isinstance(rating, Mapping) else None
        builder.set("vote_average", float(score) if score else 0)
    if not builder.get("release_date"):
        release_date = info.get("date") or info.get("air_date")
        builder.set("release_date", release_date)
        if not builder.get("year"):
            builder.set("year", str(release_date)[:4] if release_date else None)
    if not builder.get("poster_path"):
        images = info.get("images")
        poster = images.get("large") if isinstance(images, Mapping) else None
        builder.set("poster_path", poster or info.get("image"))
    builder.set_missing("overview", info.get("summary"))
    if not builder.get("names"):
        builder.set("names", _aliases(info))

    if builder.get("type") == MediaType.TV and not builder.get("seasons"):
        season = meta.begin_season if meta.begin_season is not None else 1
        raw_count = info.get("total_episodes") or info.get("eps")
        try:
            episode_count = int(raw_count) if raw_count else 0
        except (TypeError, ValueError):
            episode_count = 0
        if episode_count:
            builder.set("seasons", {season: list(range(1, episode_count + 1))})
            builder.set("number_of_episodes", episode_count)
            builder.set("number_of_seasons", 1)
    if not builder.get("genres"):
        builder.set(
            "genres",
            [
                {"id": tag.get("name"), "name": tag.get("name")}
                for tag in info.get("tags") or []
                if isinstance(tag, Mapping) and tag.get("name")
            ],
        )
    companies, directors = _companies_directors(info)
    if companies and not builder.get("production_companies"):
        builder.set("production_companies", companies)
    if directors and not builder.get("directors"):
        builder.set("directors", directors)
    builder.set_missing("actors", info.get("actors") or [])
    return builder.build()
