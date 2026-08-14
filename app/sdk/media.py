"""插件使用的媒体上下文、标题解析和识别类型。"""

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


__all__ = [
    "Context",
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
]
