"""种子缓存 API 输出模型。"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.types import MediaSource


class TorrentCacheItem(BaseModel):
    """单条站点种子缓存。"""

    hash: str
    domain: str
    title: Optional[str] = None
    description: Optional[str] = None
    size: Optional[int] = None
    pubdate: Optional[str] = None
    site_name: Optional[str] = None
    media_name: Optional[str] = None
    media_year: Optional[str | int] = None
    media_type: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    season_episode: Optional[str] = None
    resource_term: Optional[str] = None
    enclosure: Optional[str] = None
    page_url: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None


class TorrentCacheData(BaseModel):
    """种子缓存统计及明细。"""

    count: int = 0
    sites: int = 0
    data: list[TorrentCacheItem] = Field(default_factory=list)


class TorrentReidentifyData(BaseModel):
    """种子重新识别后的媒体身份。"""

    media_name: Optional[str] = None
    media_year: Optional[str | int] = None
    media_type: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
