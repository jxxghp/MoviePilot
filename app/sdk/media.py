"""插件使用的媒体上下文、标题解析、识别类型和媒体身份规则。"""

from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import (
    MetaMusic,
    MusicNameContext,
    MusicNameParseResult,
    MusicNameParser,
    MusicNamePattern,
    MusicNamePatternMatch,
    MusicNameRegistry,
)
from app.domain.meta.metavideo import MetaVideo
from app.domain.meta.words import WordsMatcher
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.domain.scraper import NfoReader
from app.domain.tokens import Tokens
from app.domain.media import (
    MUSIC_MEDIA_SOURCE_ORDER,
    MUSIC_MEDIA_SOURCES,
    configure_search_source_provider,
    is_media_source_enabled,
    is_media_source_selected,
    is_music_media_source,
    normalize_music_type,
    parse_media_source_selection,
)
from app.schemas.media import (
    MEDIA_SOURCE_ALIASES,
    MEDIA_SOURCE_PREFIXES,
    build_media_key,
    normalize_media_identity_payload,
    normalize_media_source,
    parse_media_key,
    resolve_media_identity,
)


__all__ = [
    "Context",
    "MEDIA_SOURCE_ALIASES",
    "MEDIA_SOURCE_PREFIXES",
    "MUSIC_MEDIA_SOURCE_ORDER",
    "MUSIC_MEDIA_SOURCES",
    "MediaInfo",
    "MetaAnime",
    "MetaBase",
    "MetaInfo",
    "MetaInfoPath",
    "MetaMusic",
    "MetaVideo",
    "NfoReader",
    "Tokens",
    "MusicNameContext",
    "MusicNameParseResult",
    "MusicNameParser",
    "MusicNamePattern",
    "MusicNamePatternMatch",
    "MusicNameRegistry",
    "TorrentInfo",
    "WordsMatcher",
    "build_media_key",
    "configure_search_source_provider",
    "is_media_source_enabled",
    "is_media_source_selected",
    "is_music_media_source",
    "normalize_media_identity_payload",
    "normalize_media_source",
    "normalize_music_type",
    "parse_media_key",
    "parse_media_source_selection",
    "resolve_media_identity",
]
