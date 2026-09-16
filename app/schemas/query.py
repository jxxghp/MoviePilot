"""插件只读数据查询使用的稳定筛选、分页与数据投影合同。"""

from enum import Enum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import JsonData
from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource, MediaType

T = TypeVar("T")

DEFAULT_QUERY_PAGE_SIZE = 50
MAX_QUERY_PAGE_SIZE = 200


class _QueryInput(BaseModel):  # type: ignore[misc]  # Pydantic imports are skipped by strict mypy
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
    """声明公开查询的稳定排序字段与方向。

    日期排序将空日期置于升序开头、降序末尾，并以同方向 ID 打破日期并列；ID
    本身唯一，因此按 ID 排序不依赖数据库的隐式行顺序。
    """

    field: QuerySortField = QuerySortField.DATE
    direction: QuerySortDirection = QuerySortDirection.DESC


class QueryPageRequest(_QueryInput):
    """限制单次读取规模，并通过显式排序为跨页扫描提供稳定顺序。"""

    page: int = Field(default=1, ge=1)
    count: int = Field(
        default=DEFAULT_QUERY_PAGE_SIZE,
        ge=1,
        le=MAX_QUERY_PAGE_SIZE,
    )
    sort: QuerySort = Field(default_factory=QuerySort)


class QueryPage(BaseModel, Generic[T]):  # type: ignore[misc]  # Pydantic imports are skipped by strict mypy
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


class MediaIdentityQuery(_QueryInput):
    """允许省略身份，但显式筛选时要求来源与原生 ID 成对有效。"""

    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None

    @model_validator(mode="after")  # type: ignore[misc]
    def _validate_media_identity(self) -> "MediaIdentityQuery":
        """规范化 ID，并拒绝显式半对、空白或零值身份。"""
        source_provided = "media_source" in self.model_fields_set
        id_provided = "media_id" in self.model_fields_set
        normalized_id = str(self.media_id).strip() if self.media_id is not None else None
        if source_provided != id_provided or bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        object.__setattr__(self, "media_id", normalized_id)
        return self


class _QuerySnapshot(BaseModel):  # type: ignore[misc]  # Pydantic imports are skipped by strict mypy
    """只读查询 DTO 的媒体身份与脏数据归一化边界。"""

    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("media_source", mode="before")  # type: ignore[misc]
    @classmethod
    def _normalize_source(cls, value: Any) -> Optional[MediaSource]:
        """未知旧来源不应使整页查询失败，统一降级为空身份。"""
        return normalize_media_source(value)

    @model_validator(mode="after")  # type: ignore[misc]
    def _normalize_identity_pair(self) -> "_QuerySnapshot":
        """输出中的脏半对身份按无身份处理，避免向插件传播不可用主键。"""
        normalized_id = str(self.media_id).strip() if self.media_id is not None else ""
        if not self.media_source or not normalized_id or normalized_id == "0":
            object.__setattr__(self, "media_source", None)
            object.__setattr__(self, "media_id", None)
        else:
            object.__setattr__(self, "media_id", normalized_id)
        return self


class SubscriptionSnapshot(_QuerySnapshot):
    """当前订阅的稳定只读快照，不包含 ORM 或写入行为。"""

    id: int
    name: Optional[str] = None
    year: Optional[str] = None
    type: Optional[str] = None
    keyword: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    season: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    vote: Optional[float] = None
    description: Optional[str] = None
    # 资源选择规则与质量约束均按宿主保存值只读返回。
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
    # 订阅进度字段由宿主维护，插件查询不得据此直接写库。
    total_episode: Optional[int] = None
    start_episode: Optional[int] = None
    lack_episode: Optional[int] = None
    note: Optional[JsonData] = None
    state: Optional[str] = None
    last_update: Optional[str] = None
    date: Optional[str] = None
    username: Optional[str] = None
    sites: Optional[list[int]] = None
    downloader: Optional[str] = None
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
    manual_total_episode: Optional[int] = None
    custom_words: Optional[str] = None
    media_category_id: Optional[str] = None
    media_category: Optional[str] = None
    filter_groups: Optional[list[str]] = None
    episode_group: Optional[str] = None


class SubscriptionHistorySnapshot(_QuerySnapshot):
    """订阅完成历史的稳定只读快照。"""

    id: int
    name: Optional[str] = None
    year: Optional[str] = None
    type: Optional[str] = None
    keyword: Optional[str] = None
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
    custom_words: Optional[str] = None
    media_category_id: Optional[str] = None
    media_category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    filter_groups: Optional[list[str]] = None
    episode_group: Optional[str] = None


class DownloadHistorySnapshot(_QuerySnapshot):
    """下载历史的稳定只读快照。"""

    id: int
    path: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    music_type: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    poster: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_description: Optional[str] = None
    torrent_site: Optional[str] = None
    userid: Optional[str] = None
    username: Optional[str] = None
    channel: Optional[str] = None
    date: Optional[str] = None
    note: Optional[JsonData] = None
    media_category_id: Optional[str] = None
    media_category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    episode_group: Optional[str] = None
    custom_words: Optional[str] = None


class TransferHistorySnapshot(_QuerySnapshot):
    """整理历史的稳定只读快照；内部任务结算标识不会暴露给插件。"""

    id: int
    src: Optional[str] = None
    src_storage: Optional[str] = None
    src_fileitem: Optional[JsonData] = None
    dest: Optional[str] = None
    dest_storage: Optional[str] = None
    dest_fileitem: Optional[JsonData] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    media_category_id: Optional[str] = None
    category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    status: bool = False
    errmsg: Optional[str] = None
    date: Optional[str] = None
    files: Optional[JsonData] = None
    episode_group: Optional[str] = None

    @field_validator("status", mode="before")  # type: ignore[misc]
    @classmethod
    def _normalize_status(cls, value: object) -> bool:
        """旧记录的 NULL 状态按失败处理，避免错误地向插件声明整理成功。"""
        return bool(value)


class SubscriptionFilter(MediaIdentityQuery):
    """当前订阅的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配，其余字段均精确匹配；
    ``music_type=recording`` 同时匹配未标注音乐类型的旧单曲记录。
    """

    ids: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    usernames: tuple[str, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None


class SubscriptionHistoryFilter(MediaIdentityQuery):
    """订阅完成历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配，其余字段均精确匹配；
    ``music_type=recording`` 同时匹配未标注音乐类型的旧单曲记录。
    """

    ids: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    usernames: tuple[str, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None


class DownloadHistoryFilter(MediaIdentityQuery):
    """下载历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配；除 ``text`` 外的字符串
    字段均精确匹配。``text`` 对标题和路径执行转义后的字面包含查询，不把 ``%``
    或 ``_`` 解释为通配符。``music_type=recording`` 同时匹配旧 NULL 记录。
    """

    ids: tuple[int, ...] = ()
    media_types: tuple[MediaType, ...] = ()
    title: Optional[str] = None
    text: Optional[str] = None
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
    """整理历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配；除 ``text`` 外的字符串
    字段均精确匹配。``text`` 对标题、源路径和目标路径执行转义后的字面包含查询。
    ``require_media_identity`` 只保留来源可解析且原生 ID 非空、非零的记录；
    ``status=False`` 包含旧 NULL 状态，``music_type=recording`` 包含旧 NULL 类型。
    """

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
    "DownloadHistoryFilter",
    "DownloadHistorySnapshot",
    "MediaIdentityQuery",
    "QueryPage",
    "QueryPageRequest",
    "QuerySort",
    "QuerySortDirection",
    "QuerySortField",
    "SubscriptionFilter",
    "SubscriptionHistorySnapshot",
    "SubscriptionHistoryFilter",
    "SubscriptionSnapshot",
    "TransferHistoryFilter",
    "TransferHistorySnapshot",
]
