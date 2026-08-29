"""豆瓣详情到统一媒体字段的纯投影规则。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.metainfo import MetaInfo
from app.domain.projection.mapping import ProjectionBuilder
from app.schemas.types import MediaSource, MediaType


def _media_type(info: Mapping[str, Any]) -> MediaType | None:
    """从豆瓣多种详情形态解析标准媒体类型。"""
    source_type = info.get("media_type")
    if isinstance(source_type, MediaType):
        return source_type
    subtype = info.get("subtype") or info.get("target_type")
    if subtype:
        return MediaType.MOVIE if subtype == "movie" else MediaType.TV
    if info.get("type_name"):
        return MediaType(info["type_name"])
    if info.get("uri"):
        return MediaType.MOVIE if "/movie/" in str(info["uri"]) else MediaType.TV
    if info.get("type") in {"movie", "tv"}:
        return MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
    return None


def _poster(info: Mapping[str, Any]) -> Any:
    """按豆瓣多版本响应优先级选择海报。"""
    pic = info.get("pic")
    if isinstance(pic, Mapping) and pic.get("large"):
        return pic.get("large")
    cover_url = info.get("cover_url")
    if cover_url:
        return re.sub(
            r"imageView2/\d/q/\d+/w/\d+/h/\d+/format/webp",
            "imageView2/1/w/500/h/750/format/webp",
            str(cover_url),
        )
    cover = info.get("cover")
    if isinstance(cover, Mapping):
        if cover.get("url"):
            return cover.get("url")
        large = cover.get("large")
        if isinstance(large, Mapping):
            return large.get("url")
    return None


def _overview(info: Mapping[str, Any]) -> str:
    """按豆瓣详情和榜单响应形态生成简介。"""
    overview = info.get("intro") or info.get("card_subtitle") or ""
    if overview:
        return str(overview)
    extra = info.get("extra")
    extra_info = extra.get("info") if isinstance(extra, Mapping) else None
    if not extra_info:
        return ""
    return "，".join("：".join(str(part) for part in item) for item in extra_info)


def _aliases(info: Mapping[str, Any]) -> list[str]:
    """清理豆瓣别名中的地区译名标记。"""
    return [re.sub(r"\([港台豆友译名]+\)", "", str(alias)) for alias in info.get("aka") or []]


def project(
    current: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """将只读豆瓣详情投影为统一媒体字段，不修改输入。"""
    if not info:
        return {}
    builder = ProjectionBuilder(current)
    media_id = str(info.get("id")) if info.get("id") is not None else None
    builder.set("media_source", MediaSource.Douban)
    builder.set("douban_info", info)
    builder.set("media_id", media_id)
    builder.set("douban_id", media_id)
    if not builder.get("type"):
        source_type = _media_type(info)
        if source_type:
            builder.set("type", source_type)
    builder.set_missing("title", info.get("title"))
    builder.set_missing("en_title", info.get("original_title"))
    builder.set_missing("original_title", info.get("original_title"))

    if not builder.get("year"):
        year = str(info.get("year"))[:4] if info.get("year") else None
        extra = info.get("extra")
        if not year and isinstance(extra, Mapping):
            year = extra.get("year")
        builder.set("year", year)

    meta = MetaInfo(str(info.get("title") or ""))
    if builder.get("season") is None:
        builder.set("season", meta.begin_season)
        if meta.begin_season is not None:
            builder.set("type", MediaType.TV)
        elif not builder.get("type"):
            builder.set("type", MediaType.MOVIE)

    if not builder.get("vote_average"):
        rating = info.get("rating")
        score = rating.get("value") if isinstance(rating, Mapping) else None
        builder.set("vote_average", float(score) if score else 0)
    if not builder.get("release_date"):
        release_date = info.get("release_date")
        if not release_date:
            pubdate = info.get("pubdate")
            first = pubdate[0] if isinstance(pubdate, list) and pubdate else None
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(first or ""))
            release_date = match.group() if match else None
        builder.set("release_date", release_date)
    if not builder.get("poster_path"):
        builder.set("poster_path", _poster(info))
    if not builder.get("overview"):
        builder.set("overview", _overview(info))
    if builder.get("overview") and not builder.get("year"):
        match = re.search(r"\d{4}", str(builder.get("overview")))
        if match:
            builder.set("year", match.group())
    builder.set_missing("directors", info.get("directors") or [])
    builder.set_missing("actors", info.get("actors") or [])
    if not builder.get("names"):
        builder.set("names", _aliases(info))

    if builder.get("type") == MediaType.TV and not builder.get("seasons"):
        season = meta.begin_season if meta.begin_season is not None else 1
        episode_count = info.get("episodes_count")
        if isinstance(episode_count, int) and episode_count > 0:
            builder.set("seasons", {season: list(range(1, episode_count + 1))})
    if not builder.get("season_years"):
        raw_years = info.get("season_years")
        season_years = (
            {season: str(year) for season, year in raw_years.items() if year is not None}
            if isinstance(raw_years, Mapping)
            else {}
        )
        if builder.get("type") == MediaType.TV and not season_years and builder.get("year"):
            season = builder.get("season") if builder.get("season") is not None else 1
            season_years = {season: str(builder.get("year"))}
        builder.set("season_years", season_years)
    if not builder.get("genres"):
        builder.set("genres", [{"id": genre, "name": genre} for genre in info.get("genres") or []])
    if not builder.get("runtime") and info.get("durations"):
        match = re.search(r"\d+", str(info["durations"][0]))
        if match:
            builder.set("runtime", int(match.group()))
    if not builder.get("production_countries"):
        builder.set(
            "production_countries",
            [{"id": country, "name": country} for country in info.get("countries") or []],
        )
    builder.fill_missing(info, skip=frozenset({"season_years"}))
    return builder.build()
