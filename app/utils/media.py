from typing import Any, Optional, Tuple


MEDIA_SOURCE_ALIASES = {
    "tmdb": "themoviedb",
    "themoviedb": "themoviedb",
    "douban": "douban",
    "bangumi": "bangumi",
    "anilist": "anilist",
}

MEDIA_SOURCE_PREFIXES = {
    "themoviedb": "tmdb",
    "douban": "douban",
    "bangumi": "bangumi",
    "anilist": "anilist",
}

MEDIA_SOURCE_ID_FIELDS = {
    "themoviedb": ("tmdb_id", "tmdbid"),
    "douban": ("douban_id", "doubanid"),
    "bangumi": ("bangumi_id", "bangumiid"),
    "anilist": ("anilist_id", "anilistid"),
}


def normalize_media_source(source: Optional[str]) -> Optional[str]:
    """规范化媒体数据源名称，兼容外部使用的 ``tmdb`` 前缀。"""
    if not source:
        return None
    normalized = str(source).strip().casefold()
    return MEDIA_SOURCE_ALIASES.get(normalized, normalized or None)


def parse_media_key(media_key: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """解析带来源前缀的媒体键，返回规范化数据源与原生 ID。"""
    if not media_key or ":" not in str(media_key):
        return None, None
    prefix, media_id = str(media_key).split(":", 1)
    source = normalize_media_source(prefix)
    media_id = media_id.strip()
    if not source or not media_id:
        return None, None
    return source, media_id


def resolve_media_identity(
        media: Any = None,
        source: Optional[str] = None,
        media_id: Optional[Any] = None,
        tmdbid: Optional[Any] = None,
        doubanid: Optional[Any] = None,
        bangumiid: Optional[Any] = None,
        anilistid: Optional[Any] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    从统一媒体对象、通用身份或兼容 ID 中解析主媒体身份。

    显式 ``source/media_id`` 优先；未指定来源时按 TMDB、豆瓣、Bangumi、
    AniList 的兼容顺序选择首个有效 ID。
    """
    normalized_source = normalize_media_source(source)
    if normalized_source and media_id is not None and str(media_id).strip():
        return normalized_source, str(media_id).strip()

    values = {
        "themoviedb": tmdbid,
        "douban": doubanid,
        "bangumi": bangumiid,
        "anilist": anilistid,
    }
    if media is not None:
        normalized_source = normalized_source or normalize_media_source(
            getattr(media, "source", None) or getattr(media, "media_source", None)
        )
        object_media_id = getattr(media, "media_id", None)
        if normalized_source and object_media_id is not None and str(object_media_id).strip():
            return normalized_source, str(object_media_id).strip()
        for media_source, fields in MEDIA_SOURCE_ID_FIELDS.items():
            for field in fields:
                value = getattr(media, field, None)
                if value is not None and str(value).strip():
                    values[media_source] = value
                    break

        legacy_source, legacy_media_id = parse_media_key(
            getattr(media, "mediaid", None)
        )
        if not normalized_source and legacy_source and legacy_media_id:
            return legacy_source, legacy_media_id

    if normalized_source:
        value = values.get(normalized_source)
        return (
            normalized_source,
            str(value).strip() if value is not None and str(value).strip() else None,
        )

    for media_source in MEDIA_SOURCE_ID_FIELDS:
        value = values.get(media_source)
        if value is not None and str(value).strip():
            return media_source, str(value).strip()
    return None, None


def build_media_key(source: Optional[str], media_id: Optional[Any]) -> str:
    """构造 API 使用的带来源前缀媒体键。"""
    normalized_source = normalize_media_source(source)
    if not normalized_source or media_id is None or not str(media_id).strip():
        return ""
    prefix = MEDIA_SOURCE_PREFIXES.get(normalized_source, normalized_source)
    return f"{prefix}:{str(media_id).strip()}"
