"""艺术家完整作品获取 API 模型。"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import JsonData
from app.schemas.context import TorrentInfo
from app.schemas.music import MusicInfo
from app.schemas.types import MediaSource


class ArtistCollectionProbeRequest(BaseModel):  # type: ignore[misc]
    torrent: TorrentInfo
    works: list[MusicInfo] = Field(min_length=1, max_length=500)


class ArtistWorkCoverage(BaseModel):  # type: ignore[misc]
    media_id: str
    state: Literal["confirmed", "probable", "missing"]
    evidence: str = ""


class ArtistCollectionCoverage(BaseModel):  # type: ignore[misc]
    folder_name: str = ""
    file_count: int = 0
    confirmed_count: int = 0
    probable_count: int = 0
    missing_count: int = 0
    works: list[ArtistWorkCoverage] = Field(default_factory=list)


class ArtistAcquisitionCollection(BaseModel):  # type: ignore[misc]
    torrent: TorrentInfo
    coverage: list[ArtistWorkCoverage] = Field(default_factory=list)


class ArtistAcquisitionSupplement(BaseModel):  # type: ignore[misc]
    media: MusicInfo
    torrent: TorrentInfo


class ArtistAcquisitionRequest(BaseModel):  # type: ignore[misc]
    artist_source: MediaSource
    artist_id: str = Field(min_length=1)
    artist_name: str = Field(min_length=1)
    works: list[MusicInfo] = Field(min_length=1, max_length=500)
    collection: Optional[ArtistAcquisitionCollection] = None
    supplements: list[ArtistAcquisitionSupplement] = Field(default_factory=list, max_length=500)
    normalize_source: Optional[bool] = None
    downloader: Optional[str] = None
    save_path: Optional[str] = None

    @model_validator(mode="after")  # type: ignore[misc]
    def _require_resource(self) -> "ArtistAcquisitionRequest":
        if self.collection is None and not self.supplements:
            raise ValueError("至少需要一个合集或缺失补齐资源")
        return self


class ArtistAcquisitionTask(BaseModel):  # type: ignore[misc]
    job_id: str
    state: str
    artist_source: str
    artist_id: str
    artist_name: str
    total_count: int
    covered_count: int
    supplement_count: int
    failed_count: int
    plan: dict[str, JsonData] = Field(default_factory=dict)
    results: list[dict[str, JsonData]] = Field(default_factory=list)
    created_at: str
    updated_at: str
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
