"""内置媒体来源的标准分类字段能力声明。"""

from typing import Final

from app.schemas.category import ClassificationSourceSupport
from app.schemas.types import MediaSource

FIXTURE_CLASSIFICATION_SOURCES: Final[tuple[str, ...]] = (
    MediaSource.TMDB.value,
    MediaSource.Douban.value,
    MediaSource.Bangumi.value,
    MediaSource.AniList.value,
    MediaSource.IMDb.value,
    MediaSource.TVDB.value,
    MediaSource.MusicBrainz.value,
    MediaSource.TheAudioDB.value,
    MediaSource.DoubanMusic.value,
)
"""首版分类体系具备真实标准投影 fixture 的内置来源顺序。"""

BUILTIN_CLASSIFICATION_SOURCES: Final[tuple[str, ...]] = (
    *FIXTURE_CLASSIFICATION_SOURCES,
    MediaSource.Bilibili.value,
    MediaSource.MangoTV.value,
    MediaSource.MiguVideo.value,
    MediaSource.TencentVideo.value,
    MediaSource.Iqiyi.value,
)
"""媒体来源 API 当前暴露的全部内置来源顺序。"""

STANDARD_CLASSIFICATION_FIELD_IDS: Final[tuple[str, ...]] = (
    "identity.media_source",
    "media.type",
    "media.year",
    "media.language",
    "media.countries",
    "media.genre_keys",
    "media.genre_names",
    "media.adult",
    "media.runtime",
    "media.content_rating",
    "media.companies",
    "media.networks",
    "music.entity_type",
    "music.album_type",
    "music.secondary_types",
    "music.genres",
    "music.tags",
    "music.artist_country",
    "music.release_status",
)
"""来源能力矩阵必须覆盖的标准字段稳定顺序。"""

_NATIVE: Final[ClassificationSourceSupport] = "native"
_DERIVED: Final[ClassificationSourceSupport] = "derived"
_PARTIAL: Final[ClassificationSourceSupport] = "partial"
_UNAVAILABLE: Final[ClassificationSourceSupport] = "unavailable"

_SOURCE_FIELD_SUPPORT: Final[dict[str, dict[str, ClassificationSourceSupport]]] = {
    MediaSource.TMDB.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _DERIVED,
        "media.language": _NATIVE,
        "media.countries": _PARTIAL,
        "media.genre_keys": _DERIVED,
        "media.genre_names": _PARTIAL,
        "media.adult": _NATIVE,
        "media.runtime": _PARTIAL,
        "media.content_rating": _PARTIAL,
        "media.companies": _PARTIAL,
        "media.networks": _PARTIAL,
    },
    MediaSource.Douban.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _NATIVE,
        "media.countries": _PARTIAL,
        "media.genre_keys": _PARTIAL,
        "media.genre_names": _PARTIAL,
        "media.runtime": _PARTIAL,
    },
    MediaSource.Bangumi.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _DERIVED,
        "media.genre_keys": _PARTIAL,
        "media.genre_names": _PARTIAL,
        "media.companies": _PARTIAL,
    },
    MediaSource.AniList.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _DERIVED,
        "media.language": _PARTIAL,
        "media.countries": _NATIVE,
        "media.genre_keys": _DERIVED,
        "media.genre_names": _NATIVE,
        "media.adult": _NATIVE,
        "media.runtime": _NATIVE,
        "media.companies": _NATIVE,
    },
    MediaSource.IMDb.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _NATIVE,
        "media.language": _PARTIAL,
        "media.countries": _PARTIAL,
        "media.genre_keys": _PARTIAL,
        "media.genre_names": _PARTIAL,
        "media.adult": _PARTIAL,
        "media.runtime": _PARTIAL,
    },
    MediaSource.TVDB.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _PARTIAL,
    },
    MediaSource.MusicBrainz.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _PARTIAL,
        "media.countries": _PARTIAL,
        "media.genre_keys": _PARTIAL,
        "media.genre_names": _NATIVE,
        "music.entity_type": _DERIVED,
        "music.album_type": _PARTIAL,
        "music.secondary_types": _PARTIAL,
        "music.genres": _NATIVE,
        "music.tags": _PARTIAL,
        "music.artist_country": _PARTIAL,
        "music.release_status": _PARTIAL,
    },
    MediaSource.TheAudioDB.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _PARTIAL,
        "media.countries": _PARTIAL,
        "media.genre_keys": _DERIVED,
        "media.genre_names": _DERIVED,
        "music.entity_type": _DERIVED,
        "music.album_type": _PARTIAL,
        "music.genres": _DERIVED,
        "music.tags": _PARTIAL,
        "music.artist_country": _PARTIAL,
    },
    MediaSource.DoubanMusic.value: {
        "identity.media_source": _NATIVE,
        "media.type": _DERIVED,
        "media.year": _PARTIAL,
        "media.genre_keys": _DERIVED,
        "media.genre_names": _PARTIAL,
        "music.entity_type": _DERIVED,
        "music.album_type": _PARTIAL,
        "music.genres": _PARTIAL,
        "music.tags": _PARTIAL,
    },
    MediaSource.Bilibili.value: {},
    MediaSource.MangoTV.value: {},
    MediaSource.MiguVideo.value: {},
    MediaSource.TencentVideo.value: {},
    MediaSource.Iqiyi.value: {},
}


def builtin_field_source_support(
    field_id: str,
) -> dict[str, ClassificationSourceSupport]:
    """返回一个标准字段对全部内置来源的显式支持等级。"""
    return {
        source: _SOURCE_FIELD_SUPPORT[source].get(field_id, _UNAVAILABLE)
        for source in BUILTIN_CLASSIFICATION_SOURCES
    }


def builtin_source_field_support(
    media_source: str,
) -> dict[str, ClassificationSourceSupport]:
    """返回一个内置来源对全部标准字段的显式支持等级。"""
    support = _SOURCE_FIELD_SUPPORT.get(str(media_source), {})
    return {
        field_id: support.get(field_id, _UNAVAILABLE)
        for field_id in STANDARD_CLASSIFICATION_FIELD_IDS
    }
