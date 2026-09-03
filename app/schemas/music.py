from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.category import ClassificationFactValue, ClassificationResult
from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin, RequiredMediaIdentityMixin
from app.schemas.types import MediaSource, MusicEntityType, MusicTargetEntityType


class MusicMeta(OptionalMediaIdentityMixin, BaseModel):
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
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None


class MusicInfo(OptionalMediaIdentityMixin, BaseModel):
    """标准化音乐元数据信息。"""

    type: Literal["音乐"] = "音乐"
    # 音乐实体类型：recording 单曲、album 专辑、artist 艺术家
    music_type: MusicEntityType = "recording"
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    artist: Optional[str] = None
    artist_ids: list[str] = Field(default_factory=list)
    album: Optional[str] = None
    album_artist: Optional[str] = None
    album_id: Optional[str] = None
    album_type: Optional[str] = None
    secondary_types: list[str] = Field(default_factory=list)
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
    library_category: Optional[str] = ""
    metadata_category: Optional[str] = ""
    classification: Optional[ClassificationResult] = None
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict)
    category: Optional[str] = ""
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    artist_country: Optional[str] = None
    release_status: Optional[str] = None
    names: list[str] = Field(default_factory=list)
    detail_link: Optional[str] = None
    listen_count: Optional[int] = None
    raw_data: dict[str, JsonData] = Field(default_factory=dict)
    title_year: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    overview: Optional[str] = None
    vote_average: float = 0.0

    @field_validator("album_type", mode="before")
    @classmethod
    def _strip_album_type(cls, v: Optional[str]) -> Optional[str]:
        """去除专辑类型字段两端的空白，防止数据源返回带空格的值。"""
        return v.strip() if isinstance(v, str) and v.strip() else None

    @model_validator(mode="before")  # type: ignore[misc]
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """迁移旧音乐 category 元数据，并让兼容字段只映射媒体库分类。"""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        has_library = "library_category" in payload
        has_metadata = "metadata_category" in payload
        legacy_category = payload.get("category") or ""
        if not has_library and not has_metadata and legacy_category:
            payload["metadata_category"] = legacy_category
            payload["library_category"] = ""
        library_category = payload.get("library_category") or ""
        payload["library_category"] = library_category
        payload["category"] = library_category
        return payload

    def __getattr__(self, name: str) -> None:
        """影视专用字段兜底返回 None：音乐模型不存在这些字段，避免下游逐点安全访问。

        与 domain 层的 MusicInfo 保持一致：通知、整理等共享影视字段的读写路径
        直接访问缺失属性时按空值处理，而不是抛 AttributeError。dunder 特殊方法
        除外，避免 copy/pickle 等机制对钩子的 hasattr 探测误判。
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return None


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


class MusicAlbumInfo(OptionalMediaIdentityMixin, BaseModel):
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
    artist_country: Optional[str] = None
    release_status: Optional[str] = None
    library_category: Optional[str] = ""
    metadata_category: Optional[str] = ""
    classification: Optional[ClassificationResult] = None
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict)
    category: Optional[str] = ""
    rating: float = 0.0
    rating_votes: Optional[int] = None
    detail_link: Optional[str] = None
    tracks: list[MusicInfo] = Field(default_factory=list)
    releases: list[MusicRelease] = Field(default_factory=list)
    raw_data: dict[str, JsonData] = Field(default_factory=dict)
    title_year: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    overview: Optional[str] = None
    vote_average: float = 0.0

    @field_validator("album_type", mode="before")
    @classmethod
    def _strip_album_type(cls, v: Optional[str]) -> Optional[str]:
        """去除专辑类型字段两端的空白，防止数据源返回带空格的值。"""
        return v.strip() if isinstance(v, str) and v.strip() else None

    @model_validator(mode="before")  # type: ignore[misc]
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """迁移旧专辑 category 元数据，并让兼容字段只映射媒体库分类。"""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        has_library = "library_category" in payload
        has_metadata = "metadata_category" in payload
        legacy_category = payload.get("category") or ""
        if not has_library and not has_metadata and legacy_category:
            payload["metadata_category"] = legacy_category
            payload["library_category"] = ""
        library_category = payload.get("library_category") or ""
        payload["library_category"] = library_category
        payload["category"] = library_category
        return payload


class MusicArtistInfo(OptionalMediaIdentityMixin, BaseModel):
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
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict)
    relation: Optional[str] = None
    image_url: Optional[str] = None
    detail_link: Optional[str] = None
    external_links: dict[str, str] = Field(default_factory=dict)
    album_count: Optional[int] = None
    raw_data: dict[str, JsonData] = Field(default_factory=dict)
    poster_path: Optional[str] = None
    overview: Optional[str] = None


class MusicRecognizeRequest(RequiredMediaIdentityMixin, BaseModel):
    """音乐元数据详情识别请求。"""

    media_source: MediaSource
    media_id: str
    music_type: Optional[MusicTargetEntityType] = None


class MusicRecognitionCacheItem(BaseModel):
    """单条 MusicBrainz 识别缓存。"""

    key: str
    media_id: str = ""
    title: str = ""
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    year: str | int = ""
    music_type: str = "recording"
    cover_url: str = ""


class MusicRecognitionCacheData(BaseModel):
    """MusicBrainz 识别缓存统计及明细。"""

    count: int = 0
    recognized: int = 0
    unrecognized: int = 0
    data: list[MusicRecognitionCacheItem] = Field(default_factory=list)
