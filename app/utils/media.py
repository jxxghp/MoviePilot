from typing import Any, Optional, Tuple, Union

from app.core.config import settings
from app.schemas.types import (
    MUSIC_ENTITY_TYPES,
    MUSIC_SUBSCRIBABLE_TYPES,
    MediaSource,
    MediaSourceSelection,
)

MEDIA_SOURCE_ALIASES = {
    "tmdb": MediaSource.TMDB,
    "themoviedb": MediaSource.TMDB,
    "douban": MediaSource.Douban,
    "bangumi": MediaSource.Bangumi,
    "anilist": MediaSource.AniList,
    "imdb": MediaSource.IMDb,
    "tvdb": MediaSource.TVDB,
    "musicbrainz": MediaSource.MusicBrainz,
    "theaudiodb": MediaSource.TheAudioDB,
    "audio_db": MediaSource.TheAudioDB,
    "doubanmusic": MediaSource.DoubanMusic,
    "douban_music": MediaSource.DoubanMusic,
    "bilibili": MediaSource.Bilibili,
    "mangguodiscover": MediaSource.MangoTV,
    "mango_tv": MediaSource.MangoTV,
    "migu": MediaSource.MiguVideo,
    "migu_video": MediaSource.MiguVideo,
    "tencentvideodiscover": MediaSource.TencentVideo,
    "tencent_video": MediaSource.TencentVideo,
}

MEDIA_SOURCE_PREFIXES = {
    MediaSource.TMDB: "tmdb",
    MediaSource.Douban: "douban",
    MediaSource.Bangumi: "bangumi",
    MediaSource.AniList: "anilist",
    MediaSource.IMDb: "imdb",
    MediaSource.TVDB: "tvdb",
    MediaSource.MusicBrainz: "musicbrainz",
    MediaSource.TheAudioDB: "theaudiodb",
    MediaSource.DoubanMusic: "doubanmusic",
    MediaSource.Bilibili: "bilibili",
    MediaSource.MangoTV: "mangguodiscover",
    MediaSource.MiguVideo: "migu",
    MediaSource.TencentVideo: "tencentvideodiscover",
}

MUSIC_MEDIA_SOURCE_ORDER = (
    MediaSource.MusicBrainz,
    MediaSource.TheAudioDB,
    MediaSource.DoubanMusic,
)
MUSIC_MEDIA_SOURCES = frozenset(MUSIC_MEDIA_SOURCE_ORDER)


def normalize_music_type(
        value: Optional[object],
        *,
        allow_artist: bool = True,
) -> Optional[str]:
    """规范化音乐实体类型，非法值返回 None。"""
    normalized = str(value or "").strip().lower()
    allowed = MUSIC_ENTITY_TYPES if allow_artist else MUSIC_SUBSCRIBABLE_TYPES
    return normalized if normalized in allowed else None


def is_music_media_source(
        source: Optional[Union[MediaSource, str]],
) -> bool:
    """判断单个请求级来源是否为内置音乐元数据源。"""
    return normalize_media_source(source) in MUSIC_MEDIA_SOURCES


def normalize_media_source(
        source: Optional[Union[MediaSource, str]],
) -> Optional[MediaSource]:
    """将来源别名规范化为固定枚举，未知来源返回 None。"""
    if not source:
        return None
    if isinstance(source, MediaSource):
        return source
    normalized = str(source).strip().casefold()
    return MEDIA_SOURCE_ALIASES.get(normalized)


def parse_media_source_selection(value: Optional[str]) -> Tuple[MediaSource, ...]:
    """
    解析 HTTP 查询参数中的逗号分隔来源，并转换为有序枚举集合。

    :param value: 逗号分隔的来源值；空值表示未显式选择来源
    :return: 去重后的媒体来源枚举元组
    :raises ValueError: 包含固定枚举之外的来源
    """
    if not value:
        return ()
    sources: list[MediaSource] = []
    invalid_sources: list[str] = []
    for item in str(value).split(","):
        raw_source = item.strip()
        if not raw_source:
            continue
        source = normalize_media_source(raw_source)
        if not source:
            invalid_sources.append(raw_source)
        elif source not in sources:
            sources.append(source)
    if invalid_sources:
        raise ValueError(f"不支持的媒体数据源：{', '.join(invalid_sources)}")
    return tuple(sources)


def is_media_source_selected(
        media_source: Optional[MediaSourceSelection],
        source_key: MediaSource,
) -> bool:
    """
    判断请求级媒体数据源集合是否包含当前模块。

    :param media_source: 请求级媒体数据源枚举或枚举元组，空表示不作限制
    :param source_key: 当前模块对应的数据源标识
    :return: 是否包含
    """
    if not media_source:
        return True
    selected_sources = (
        (media_source,)
        if isinstance(media_source, MediaSource)
        else media_source
    )
    return source_key in selected_sources


def is_media_source_enabled(
        media_source: Optional[MediaSourceSelection],
        source_key: MediaSource,
) -> bool:
    """
    判断媒体搜索时数据源是否启用：请求级来源集合优先，未指定时回退到
    全局 SEARCH_SOURCE 多来源配置，两者均未配置时全部启用。

    :param media_source: 请求级媒体数据源枚举或枚举元组
    :param source_key: 当前模块对应的数据源标识
    :return: 是否启用
    """
    if media_source:
        return is_media_source_selected(media_source, source_key)
    if settings.SEARCH_SOURCE:
        configured_sources = {
            normalize_media_source(item)
            for item in str(settings.SEARCH_SOURCE).split(",")
        }
        return source_key in configured_sources
    return True


def parse_media_key(
        media_key: Optional[str],
) -> Tuple[Optional[MediaSource], Optional[str]]:
    """解析带来源前缀的媒体键，返回规范化数据源与原生 ID。"""
    if not media_key or ":" not in str(media_key):
        return None, None
    prefix, media_id = str(media_key).split(":", 1)
    source = normalize_media_source(prefix)
    media_id = media_id.strip()
    if not source or not media_id or media_id == "0":
        return None, None
    return source, media_id


def resolve_media_identity(
        media: Any = None,
        media_source: Optional[Union[MediaSource, str]] = None,
        media_id: Optional[Any] = None,
) -> Tuple[Optional[MediaSource], Optional[str]]:
    """
    从统一媒体对象或显式字段解析主媒体身份。

    :param media: 包含 ``media_source`` 和 ``media_id`` 的媒体对象
    :param media_source: 显式媒体来源
    :param media_id: 显式来源原生 ID
    :return: 枚举化来源和字符串 ID；任一字段无效时返回空身份
    """
    normalized_source = normalize_media_source(media_source)
    if media_source is not None or media_id is not None:
        normalized_id = str(media_id).strip() if media_id is not None else ""
        if normalized_source and normalized_id and normalized_id != "0":
            return normalized_source, normalized_id
        return None, None

    if media is None:
        return None, None
    normalized_source = normalize_media_source(
        getattr(media, "media_source", None)
        if not isinstance(media, dict)
        else media.get("media_source")
    )
    object_media_id = (
        getattr(media, "media_id", None)
        if not isinstance(media, dict)
        else media.get("media_id")
    )
    if normalized_source and object_media_id is not None:
        normalized_id = str(object_media_id).strip()
        if normalized_id and normalized_id != "0":
            return normalized_source, normalized_id
    return None, None


def normalize_media_identity_payload(
        payload: dict[str, Any],
        *,
        include_empty: bool = False,
) -> dict[str, Any]:
    """
    规范化字典中的媒体身份，保证来源与 ID 始终成对写入。

    :param payload: 待写入或传输的字段字典
    :param include_empty: 字典未声明身份字段时，是否仍补充空身份
    :return: 复制后的规范字典；非法、半对或零值身份会被清空
    """
    normalized = dict(payload)
    has_identity = "media_source" in normalized or "media_id" in normalized
    if not has_identity and not include_empty:
        return normalized
    media_source, media_id = resolve_media_identity(
        media_source=normalized.get("media_source"),
        media_id=normalized.get("media_id"),
    )
    normalized["media_source"] = media_source.value if media_source else None
    normalized["media_id"] = media_id
    return normalized


def build_media_key(
        media_source: Optional[Union[MediaSource, str]],
        media_id: Optional[Any],
) -> str:
    """构造 API 使用的带来源前缀媒体键。"""
    normalized_source = normalize_media_source(media_source)
    normalized_id = str(media_id).strip() if media_id is not None else ""
    if not normalized_source or not normalized_id or normalized_id == "0":
        return ""
    prefix = MEDIA_SOURCE_PREFIXES[normalized_source]
    return f"{prefix}:{normalized_id}"
