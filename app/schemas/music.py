from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


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
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    duration: Optional[int] = None
    isrc: Optional[str] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None


class MusicInfo(BaseModel):
    """标准化音乐元数据信息。"""

    type: Literal["音乐"] = "音乐"
    source: Optional[str] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
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
    category: Optional[str] = ""
    names: list[str] = Field(default_factory=list)
    detail_link: Optional[str] = None
    listen_count: Optional[int] = None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    title_year: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    mediaid_prefix: Optional[str] = None
    overview: Optional[str] = None
    vote_average: float = 0.0


class MusicRecognizeRequest(BaseModel):
    """音乐元数据详情识别请求。"""

    source: str
    media_id: str
