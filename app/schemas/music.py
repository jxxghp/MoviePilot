from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.schemas.types import MediaSource, MusicEntityType, MusicTargetEntityType


class MusicMeta(BaseModel):
    """音乐名称及音频文件解析结果。"""

    type: Literal["音乐"] = "音乐"
    org_string: Optional[str] = None
    title: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    year: Optional[int] = None
    disc_number: Optional[int] = None
    track_number: Optional[int] = None
    total_discs: Optional[int] = None
    total_tracks: Optional[int] = None
    version: Optional[str] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    audio_quality: Optional[Literal["hires", "lossless", "lossy"]] = None
    audio_quality_score: int = 0
    audio_specs: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    duration: Optional[int] = None
    isrc: Optional[str] = None
    media_source: Optional[Union[MediaSource, str]] = None
    media_id: Optional[str] = None


class MusicInfo(BaseModel):
    """标准化音乐元数据信息。"""

    type: Literal["音乐"] = "音乐"
    # 音乐实体类型：recording 单曲、album 专辑、artist 艺术家
    music_type: MusicEntityType = "recording"
    media_source: Optional[Union[MediaSource, str]] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    artist: Optional[str] = None
    artist_ids: list[str] = Field(default_factory=list)
    album: Optional[str] = None
    album_artist: Optional[str] = None
    album_id: Optional[str] = None
    album_type: Optional[str] = None
    year: Optional[int] = None
    release_date: Optional[str] = None
    disc_number: Optional[int] = None
    track_number: Optional[int] = None
    total_tracks: Optional[int] = None
    duration: Optional[int] = None
    isrc: Optional[str] = None
    cover_url: Optional[str] = None
    lyrics: Optional[str] = None
    version: Optional[str] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    audio_quality: Optional[Literal["hires", "lossless", "lossy"]] = None
    audio_quality_score: int = 0
    audio_specs: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    category: Optional[str] = ""
    genres: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    detail_link: Optional[str] = None
    listen_count: Optional[int] = None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    title_year: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    overview: Optional[str] = None
    vote_average: float = 0.0


class MusicRelease(BaseModel):
    """音乐专辑下的单个发行版本。"""

    media_id: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    year: Optional[int] = None
    country: Optional[str] = None
    status: Optional[str] = None
    packaging: Optional[str] = None
    formats: list[str] = Field(default_factory=list)
    track_count: Optional[int] = None
    cover_url: Optional[str] = None


class MusicAlbumInfo(BaseModel):
    """标准化音乐专辑信息。"""

    type: Literal["音乐"] = "音乐"
    music_type: Literal["album"] = "album"
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    artist: Optional[str] = None
    artist_ids: list[str] = Field(default_factory=list)
    album: Optional[str] = None
    album_type: Optional[str] = None
    secondary_types: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    release_date: Optional[str] = None
    total_tracks: Optional[int] = None
    duration: Optional[int] = None
    cover_url: Optional[str] = None
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    category: Optional[str] = ""
    rating: float = 0.0
    rating_votes: Optional[int] = None
    detail_link: Optional[str] = None
    tracks: list[MusicInfo] = Field(default_factory=list)
    releases: list[MusicRelease] = Field(default_factory=list)
    raw_data: dict[str, Any] = Field(default_factory=dict)
    title_year: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    overview: Optional[str] = None
    vote_average: float = 0.0


class MusicArtistInfo(BaseModel):
    """标准化音乐艺术家信息。"""

    type: Literal["音乐"] = "音乐"
    music_type: Literal["artist"] = "artist"
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    name: Optional[str] = None
    title: Optional[str] = None
    sort_name: Optional[str] = None
    disambiguation: Optional[str] = None
    artist_type: Optional[str] = None
    gender: Optional[str] = None
    country: Optional[str] = None
    area: Optional[str] = None
    begin_date: Optional[str] = None
    end_date: Optional[str] = None
    ended: bool = False
    life_span: Optional[str] = None
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    relation: Optional[str] = None
    image_url: Optional[str] = None
    detail_link: Optional[str] = None
    external_links: dict[str, str] = Field(default_factory=dict)
    album_count: Optional[int] = None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    poster_path: Optional[str] = None
    overview: Optional[str] = None


class MusicRecognizeRequest(BaseModel):
    """音乐元数据详情识别请求。"""

    media_source: MediaSource
    media_id: str
    music_type: Optional[MusicTargetEntityType] = None
