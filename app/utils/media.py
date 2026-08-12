from typing import Any, Optional, Tuple, Union

from app.core.config import settings
from app.schemas.types import (
    MUSIC_ENTITY_TYPES,
    MUSIC_SUBSCRIBABLE_TYPES,
    MediaSource,
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


def is_media_source_selected(
        media_source: Optional[Union[MediaSource, str]],
        source_key: Union[MediaSource, str],
) -> bool:
    """
    判断请求级媒体数据源集合是否包含当前模块。

    :param media_source: 请求级媒体数据源，支持逗号分隔，空表示不作限制
    :param source_key: 当前模块对应的数据源标识
    :return: 是否包含
    """
    if not media_source:
        return True
    normalized_key = normalize_media_source(source_key)
    selected_sources = {
        normalize_media_source(item)
        for item in str(media_source).split(",")
    }
    return bool(normalized_key and normalized_key in selected_sources)


def is_media_source_enabled(
        media_source: Optional[Union[MediaSource, str]],
        source_key: Union[MediaSource, str],
) -> bool:
    """
    判断媒体搜索时数据源是否启用：请求级来源集合优先，未指定时回退到
    全局 SEARCH_SOURCE 多来源配置，两者均未配置时全部启用。

    :param media_source: 请求级媒体数据源，支持逗号分隔
    :param source_key: 当前模块对应的数据源标识
    :return: 是否启用
    """
    if media_source:
        return is_media_source_selected(media_source, source_key)
    if settings.SEARCH_SOURCE:
        normalized_key = normalize_media_source(source_key)
        configured_sources = {
            normalize_media_source(item)
            for item in str(settings.SEARCH_SOURCE).split(",")
        }
        return normalized_key in configured_sources
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
    if not source or not media_id:
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
        if normalized_source and media_id is not None and str(media_id).strip():
            return normalized_source, str(media_id).strip()
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
        if normalized_id:
            return normalized_source, normalized_id
    return None, None


def build_media_key(
        media_source: Optional[Union[MediaSource, str]],
        media_id: Optional[Any],
) -> str:
    """构造 API 使用的带来源前缀媒体键。"""
    normalized_source = normalize_media_source(media_source)
    if not normalized_source or media_id is None or not str(media_id).strip():
        return ""
    prefix = MEDIA_SOURCE_PREFIXES[normalized_source]
    return f"{prefix}:{str(media_id).strip()}"
