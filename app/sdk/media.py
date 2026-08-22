"""插件使用的媒体上下文、标题解析、识别类型和媒体身份规则。

``MetaParseTrace`` 挂在每个识别结果的 ``parse_trace`` 上，记这次识别经过哪些解析器、哪一步
改写了哪个字段；它与 ``MetaParserRun``、``MetaFieldRevision``、``MetaParseStatus`` 同属一族，
读取轨迹要逐层往下取，因此四个一并给出。
"""

from app.domain.context import (
    Context,
    MediaInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    MusicLyrics,
    MusicRelease,
    SubtitleInfo,
    TorrentInfo,
)
from app.domain.meta.customization import CustomizationMatcher, get_customization
from app.domain.meta.infopath import (
    clear_parsed_title_for_parent_merge,
    should_use_parent_title_for_file_stem,
)
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
    audio_quality_score,
    audio_quality_tier,
    format_audio_quality,
    infer_audio_lossless,
    normalize_audio_format,
    parse_audio_quality,
)
from app.domain.meta.metavideo import MetaVideo
from app.domain.meta.releasegroup import ReleaseGroupsMatcher, get_custom_release_groups
from app.domain.meta.streamingplatform import StreamingPlatforms
from app.domain.meta.words import WordsMatcher, get_custom_words
from app.domain.metainfo import MetaInfo, MetaInfoPath, find_metainfo, is_anime
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
from app.schemas.metaparse import (
    MetaFieldRevision,
    MetaParserRun,
    MetaParseStatus,
    MetaParseTrace,
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
    "CustomizationMatcher",
    "MEDIA_SOURCE_ALIASES",
    "MEDIA_SOURCE_PREFIXES",
    "MUSIC_MEDIA_SOURCE_ORDER",
    "MUSIC_MEDIA_SOURCES",
    "MediaInfo",
    "MetaAnime",
    "MetaBase",
    "MetaFieldRevision",
    "MetaInfo",
    "MetaInfoPath",
    "MetaMusic",
    "MetaParseStatus",
    "MetaParseTrace",
    "MetaParserRun",
    "MetaVideo",
    "MusicAlbumInfo",
    "MusicArtistInfo",
    "MusicInfo",
    "MusicLyrics",
    "MusicRelease",
    "NfoReader",
    "ReleaseGroupsMatcher",
    "StreamingPlatforms",
    "SubtitleInfo",
    "Tokens",
    "MusicNameContext",
    "MusicNameParseResult",
    "MusicNameParser",
    "MusicNamePattern",
    "MusicNamePatternMatch",
    "MusicNameRegistry",
    "TorrentInfo",
    "WordsMatcher",
    "audio_quality_score",
    "audio_quality_tier",
    "build_media_key",
    "clear_parsed_title_for_parent_merge",
    "configure_search_source_provider",
    "find_metainfo",
    "format_audio_quality",
    "get_custom_release_groups",
    "get_custom_words",
    "get_customization",
    "infer_audio_lossless",
    "is_anime",
    "is_media_source_enabled",
    "is_media_source_selected",
    "is_music_media_source",
    "normalize_audio_format",
    "normalize_media_identity_payload",
    "normalize_media_source",
    "normalize_music_type",
    "parse_audio_quality",
    "parse_media_key",
    "parse_media_source_selection",
    "resolve_media_identity",
    "should_use_parent_title_for_file_stem",
]
