"""插件只读数据查询使用的稳定筛选、分页与数据投影合同。"""

from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData
from app.schemas.history import DownloadHistory, TransferHistory
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaSource, MediaType

T = TypeVar("T")

DEFAULT_QUERY_PAGE_SIZE = 50
MAX_QUERY_PAGE_SIZE = 200


class _QueryInput(BaseModel):
    """查询输入共同的严格字段合同。"""

    model_config = ConfigDict(extra="forbid")


class QuerySortField(str, Enum):
    """所有公开数据查询共同支持的稳定排序字段。"""

    DATE = "date"
    ID = "id"


class QuerySortDirection(str, Enum):
    """公开查询的排序方向。"""

    ASC = "asc"
    DESC = "desc"


class QuerySort(_QueryInput):
    """声明公开查询的稳定排序字段与方向。"""

    field: QuerySortField = QuerySortField.DATE
    direction: QuerySortDirection = QuerySortDirection.DESC


class QueryPageRequest(_QueryInput):
    """限制插件单次读取规模，并为跨页扫描提供稳定顺序。"""

    page: int = Field(default=1, ge=1)
    count: int = Field(
        default=DEFAULT_QUERY_PAGE_SIZE,
        ge=1,
        le=MAX_QUERY_PAGE_SIZE,
    )
    sort: QuerySort = Field(default_factory=QuerySort)


class QueryPage(BaseModel, Generic[T]):
    """公开查询返回的分页 DTO。"""

    items: list[T] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    page: int = Field(default=1, ge=1)
    count: int = Field(
        default=DEFAULT_QUERY_PAGE_SIZE,
        ge=1,
        le=MAX_QUERY_PAGE_SIZE,
    )

    @property
    def has_next(self) -> bool:
        """返回当前分页后是否仍有记录。"""
        return self.page * self.count < self.total


class MediaIdentityQuery(OptionalMediaIdentityMixin, _QueryInput):
    """允许省略身份，但显式筛选时要求来源与原生 ID 成对有效。"""

    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None


class SubscribeHistory(OptionalMediaIdentityMixin, BaseModel):
    """订阅完成历史的稳定只读投影。"""

    id: int
    name: Optional[str] = None
    year: Optional[str] = None
    type: Optional[str] = None
    keyword: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    season: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    vote: Optional[float] = None
    description: Optional[str] = None
    filter: Optional[str] = None
    include: Optional[str] = None
    exclude: Optional[str] = None
    quality: Optional[str] = None
    resolution: Optional[str] = None
    effect: Optional[str] = None
    audio_quality: Optional[str] = None
    audio_format: Optional[str] = None
    min_bitrate: Optional[int] = None
    min_bit_depth: Optional[int] = None
    min_sample_rate: Optional[int] = None
    total_episode: Optional[int] = None
    start_episode: Optional[int] = None
    date: Optional[str] = None
    username: Optional[str] = None
    sites: Optional[list[int]] = None
    best_version: Optional[int] = None
    best_version_full: Optional[int] = None
    current_priority: Optional[int] = None
    current_audio_format: Optional[str] = None
    current_bitrate: Optional[int] = None
    current_bit_depth: Optional[int] = None
    current_sample_rate: Optional[int] = None
    episode_priority: Optional[dict[str, int]] = None
    save_path: Optional[str] = None
    search_imdbid: Optional[int] = None
    note: Optional[JsonData] = None
    custom_words: Optional[str] = None
    media_category: Optional[str] = None
    filter_groups: Optional[list[str]] = None
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionFilter(MediaIdentityQuery):
    """订阅查询允许组合的稳定业务字段。"""

    ids: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    usernames: tuple[str, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None


class SubscriptionHistoryFilter(MediaIdentityQuery):
    """订阅完成历史查询允许组合的稳定业务字段。"""

    ids: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    usernames: tuple[str, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None


class DownloadHistoryFilter(MediaIdentityQuery):
    """下载历史查询允许组合的稳定业务字段。"""

    ids: tuple[int, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    title: Optional[str] = None
    year: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    path: Optional[str] = None
    download_hash: Optional[str] = None
    username: Optional[str] = None
    usernames: tuple[str, ...] = ()
    music_type: Optional[str] = None
    episode_group: Optional[str] = None


class TransferHistoryFilter(MediaIdentityQuery):
    """整理历史查询允许组合的稳定业务字段。"""

    ids: tuple[int, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    media_sources: tuple[MediaSource, ...] = ()
    require_media_identity: bool = False
    title: Optional[str] = None
    text: Optional[str] = None
    year: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    src: Optional[str] = None
    dest: Optional[str] = None
    status: Optional[bool] = None
    download_hash: Optional[str] = None
    music_type: Optional[str] = None
    episode_group: Optional[str] = None


__all__ = [
    "DEFAULT_QUERY_PAGE_SIZE",
    "MAX_QUERY_PAGE_SIZE",
    "DownloadHistory",
    "DownloadHistoryFilter",
    "MediaIdentityQuery",
    "QueryPage",
    "QueryPageRequest",
    "QuerySort",
    "QuerySortDirection",
    "QuerySortField",
    "Subscribe",
    "SubscribeHistory",
    "SubscriptionFilter",
    "SubscriptionHistoryFilter",
    "TransferHistory",
    "TransferHistoryFilter",
]
