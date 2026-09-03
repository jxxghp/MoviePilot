"""音乐实体面向 API 和 Agent 的有界结果投影。"""

from typing import Any

from app.domain.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.schemas.types import media_type_to_agent

MUSIC_TRACK_PREVIEW_LIMIT = 100
MUSIC_RELEASE_PREVIEW_LIMIT = 20


def simplify_music_info(info: MusicInfo) -> dict[str, Any]:
    """精简音乐列表项，同时保留订阅和下载所需的稳定身份。"""
    payload = {
        "title": info.title,
        "year": info.year,
        "type": media_type_to_agent(info.type),
        "music_type": info.music_type,
        "artists": list(info.artists or []),
        "artist_ids": list(info.artist_ids or []),
        "artist": info.artist,
        "album": info.album,
        "album_id": info.album_id,
        "album_type": info.album_type,
        "release_date": info.release_date,
        "disc_number": info.disc_number,
        "track_number": info.track_number,
        "total_tracks": info.total_tracks,
        "duration": info.duration,
        "isrc": info.isrc,
        "version": info.version,
        "genres": list(info.genres or []),
        "secondary_types": list(info.secondary_types or []),
        "tags": list(info.tags or []),
        "artist_country": info.artist_country,
        "release_status": info.release_status,
        "metadata_category": info.metadata_category,
        "library_category": info.library_category,
        "category": info.category,
        "classification": (
            info.classification.model_dump(mode="json")
            if info.classification
            else None
        ),
        "listen_count": info.listen_count,
        "media_source": info.media_source,
        "media_id": info.media_id,
        "poster_path": info.poster_path,
        "detail_link": info.detail_link,
        "overview": info.overview,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def simplify_music_album(
    info: MusicAlbumInfo,
    *,
    track_limit: int = MUSIC_TRACK_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """精简专辑详情并限制曲目和发行版本预览，避免撑大 Agent 上下文。"""
    normalized_track_limit = max(1, min(track_limit, MUSIC_TRACK_PREVIEW_LIMIT))
    tracks = list(info.tracks or [])
    releases = list(info.releases or [])
    payload = simplify_music_info(info.to_music_info())
    payload.update(
        {
            "release_date": info.release_date,
            "secondary_types": list(info.secondary_types or []),
            "tags": list(info.tags or []),
            "rating": info.rating,
            "rating_votes": info.rating_votes,
            "tracks": [simplify_music_info(track) for track in tracks[:normalized_track_limit]],
            "tracks_total": len(tracks),
            "tracks_truncated": len(tracks) > normalized_track_limit,
            "releases": [release.to_dict() for release in releases[:MUSIC_RELEASE_PREVIEW_LIMIT]],
            "releases_total": len(releases),
            "releases_truncated": len(releases) > MUSIC_RELEASE_PREVIEW_LIMIT,
        }
    )
    return payload


def simplify_music_artist(info: MusicArtistInfo) -> dict[str, Any]:
    """精简艺术家详情，明确其仅用于浏览而非订阅或下载。"""
    payload = info.to_dict()
    payload.pop("raw_data", None)
    payload.pop("mediaid_prefix", None)
    payload["type"] = media_type_to_agent(info.type)
    payload["media_source"] = info.media_source
    payload["media_id"] = info.media_id
    payload["subscribable"] = False
    return {key: value for key, value in payload.items() if value not in (None, "", [])}
