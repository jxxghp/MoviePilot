"""AniList 详情到统一媒体字段的纯投影规则。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

from app.domain.metainfo import MetaInfo
from app.domain.projection.mapping import ProjectionBuilder
from app.schemas.types import MediaSource, MediaType

MOVIE_FORMATS = frozenset({"MOVIE"})
CHINESE_TITLE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
JAPANESE_KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")


def resolve_media_type(info: Mapping[str, Any]) -> MediaType:
    """根据 AniList 发布格式返回标准媒体类型。"""
    return MediaType.MOVIE if str(info.get("format") or "").upper() in MOVIE_FORMATS else MediaType.TV


def format_date(date_info: Mapping[str, Any]) -> Optional[str]:
    """将 AniList 模糊日期转换为 YYYY、YYYY-MM 或 YYYY-MM-DD。"""
    if not date_info or not date_info.get("year"):
        return None
    values = [str(date_info.get("year"))]
    if date_info.get("month"):
        values.append(str(date_info.get("month")).zfill(2))
    if date_info.get("day"):
        values.append(str(date_info.get("day")).zfill(2))
    return "-".join(values)


def select_chinese_title(info: Mapping[str, Any]) -> Optional[str]:
    """从 anilist-chinese 标题和别名中选择中文标题。"""
    titles = info.get("title")
    translated = titles.get("chinese") if isinstance(titles, Mapping) else None
    if not translated:
        return None
    if CHINESE_TITLE_PATTERN.search(str(translated)) and not JAPANESE_KANA_PATTERN.search(str(translated)):
        return str(translated)
    for synonym in reversed(info.get("synonyms") or []):
        if CHINESE_TITLE_PATTERN.search(str(synonym)) and not JAPANESE_KANA_PATTERN.search(str(synonym)):
            return str(synonym)
    return str(translated)


def project(
    current: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """将只读 AniList 详情投影为统一媒体字段，不修改输入。"""
    if not info:
        return {}
    builder = ProjectionBuilder(current)
    media_id = str(info.get("id")) if info.get("id") is not None else None
    builder.set("media_source", MediaSource.AniList)
    builder.set("anilist_info", info)
    builder.set("media_id", media_id)
    builder.set("anilist_id", info.get("id"))
    if not builder.get("type"):
        builder.set("type", resolve_media_type(info))

    raw_titles = info.get("title")
    titles = raw_titles if isinstance(raw_titles, Mapping) else {}
    title = (
        builder.get("title")
        or select_chinese_title(info)
        or titles.get("native")
        or titles.get("romaji")
        or titles.get("english")
    )
    builder.set("title", title)
    builder.set_missing("en_title", titles.get("english"))
    builder.set_missing("original_title", titles.get("native") or titles.get("romaji"))
    builder.set(
        "names",
        list(
            dict.fromkeys(
                value
                for value in [
                    titles.get("english"),
                    titles.get("romaji"),
                    titles.get("native"),
                    *(info.get("synonyms") or []),
                ]
                if value and value != title
            )
        ),
    )

    start_date = info.get("startDate")
    end_date = info.get("endDate")
    release_date = builder.get("release_date") or format_date(start_date if isinstance(start_date, Mapping) else {})
    builder.set("release_date", release_date)
    builder.set_missing("first_air_date", release_date)
    builder.set_missing("last_air_date", format_date(end_date if isinstance(end_date, Mapping) else {}))
    if not builder.get("year"):
        start_year = start_date.get("year") if isinstance(start_date, Mapping) else None
        year = str(start_year) if start_year else str(info.get("seasonYear")) if info.get("seasonYear") else None
        builder.set("year", year)

    cover = info.get("coverImage")
    if isinstance(cover, Mapping):
        builder.set_missing("poster_path", cover.get("extraLarge") or cover.get("large"))
    builder.set_missing("backdrop_path", info.get("bannerImage"))
    if not builder.get("overview"):
        overview = re.sub(
            r"<[^>]+>",
            "",
            str(info.get("description") or "").replace("<br>", "\n").replace("<br />", "\n"),
        ).strip()
        builder.set("overview", overview)
    if not builder.get("vote_average"):
        average_score = info.get("averageScore")
        builder.set("vote_average", round(float(average_score) / 10, 1) if average_score is not None else 0)
    builder.set_missing("popularity", info.get("popularity"))
    builder.set_missing("runtime", info.get("duration"))
    builder.set("adult", builder.get("adult") or bool(info.get("isAdult")))
    builder.set_missing("status", info.get("status"))
    if not builder.get("original_language") and info.get("countryOfOrigin") == "JP":
        builder.set("original_language", "ja")
    if not builder.get("origin_country") and info.get("countryOfOrigin"):
        builder.set("origin_country", [info.get("countryOfOrigin")])
    if not builder.get("production_companies"):
        studios = info.get("studios")
        nodes = studios.get("nodes") if isinstance(studios, Mapping) else []
        builder.set(
            "production_companies",
            [
                {"name": studio.get("name")}
                for studio in nodes or []
                if isinstance(studio, Mapping) and studio.get("name")
            ],
        )
    if not builder.get("genres"):
        builder.set("genres", [{"id": genre, "name": genre} for genre in info.get("genres") or []])
    builder.set_missing("actors", info.get("actors") or [])
    builder.set_missing("directors", info.get("directors") or [])

    if builder.get("season") is None:
        builder.set("season", MetaInfo(str(title)).begin_season if title else None)
    episode_count = info.get("episodes")
    if builder.get("type") == MediaType.TV and isinstance(episode_count, int) and episode_count > 0:
        season = builder.get("season") if builder.get("season") is not None else 1
        builder.set("seasons", {season: list(range(1, episode_count + 1))})
        builder.set("number_of_episodes", episode_count)
        builder.set("number_of_seasons", 1)
        if builder.get("year"):
            builder.set("season_years", {season: str(builder.get("year"))})

    for external_link in info.get("externalLinks") or []:
        if not isinstance(external_link, Mapping) or str(external_link.get("site") or "").casefold() != "anidb":
            continue
        match = re.search(r"\d+", str(external_link.get("url") or ""))
        if match:
            builder.set("anidb_id", int(match.group()))
            break
    return builder.build()
