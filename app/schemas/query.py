"""
插件只读数据查询使用的稳定筛选、分页与数据投影合同。
"""

from enum import Enum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import JsonData
from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource, MediaType

T = TypeVar("T")

DEFAULT_QUERY_PAGE_SIZE = 50
MAX_QUERY_PAGE_SIZE = 200


class _QueryInput(BaseModel):
    """
    查询输入共同的严格字段合同。
    """

    model_config = ConfigDict(extra="forbid")


class QuerySortField(str, Enum):
    """
    所有公开数据查询共同支持的稳定排序字段。
    """

    # 按记录日期排序
    DATE = "date"
    # 按记录标识排序
    ID = "id"


class QuerySortDirection(str, Enum):
    """
    公开查询的排序方向。
    """

    # 按字段值升序排列
    ASC = "asc"
    # 按字段值降序排列
    DESC = "desc"


class QuerySort(_QueryInput):
    """
    声明公开查询的稳定排序字段与方向。

    日期排序将空日期置于升序开头、降序末尾，并以同方向 ID 打破日期并列；ID
    本身唯一，因此按 ID 排序不依赖数据库的隐式行顺序。
    """

    field: QuerySortField = Field(default=QuerySortField.DATE, description="排序依据的字段")
    direction: QuerySortDirection = Field(default=QuerySortDirection.DESC, description="排序方向，支持升序和降序")


class QueryPageRequest(_QueryInput):
    """
    限制单次读取规模，并通过显式排序为跨页扫描提供稳定顺序。
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    count: int = Field(
        default=DEFAULT_QUERY_PAGE_SIZE,
        ge=1,
        le=MAX_QUERY_PAGE_SIZE, description="每页记录数，默认 50，范围为 1 至 200")
    sort: QuerySort = Field(default_factory=QuerySort, description="分页查询的排序规则，默认按日期降序")


class QueryPage(BaseModel, Generic[T]):
    """
    公开查询返回的分页 DTO。
    """

    items: list[T] = Field(default_factory=list, description="当前页条目列表")
    total: int = Field(default=0, ge=0, description="总条目数量")
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    count: int = Field(
        default=DEFAULT_QUERY_PAGE_SIZE,
        ge=1,
        le=MAX_QUERY_PAGE_SIZE, description="每页记录数，默认 50，范围为 1 至 200")

    @property
    def has_next(self) -> bool:
        """
        返回当前分页后是否仍有记录。
        """
        return self.page * self.count < self.total


class MediaIdentityQuery(_QueryInput):
    """
    允许省略身份，但显式筛选时要求来源与原生 ID 成对有效。
    """

    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")

    @model_validator(mode="after")
    def _validate_media_identity(self) -> "MediaIdentityQuery":
        """
        规范化 ID，并拒绝显式半对、空白或零值身份。
        """
        source_provided = "media_source" in self.model_fields_set
        id_provided = "media_id" in self.model_fields_set
        normalized_id = str(self.media_id).strip() if self.media_id is not None else None
        if source_provided != id_provided or bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        object.__setattr__(self, "media_id", normalized_id)
        return self


class _QuerySnapshot(BaseModel):
    """
    只读查询 DTO 的媒体身份与脏数据归一化边界。
    """

    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")

    model_config = ConfigDict(from_attributes=True)

    @field_validator("media_source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> Optional[MediaSource]:
        """
        未知旧来源不应使整页查询失败，统一降级为空身份。
        """
        return normalize_media_source(value)

    @model_validator(mode="after")
    def _normalize_identity_pair(self) -> "_QuerySnapshot":
        """
        输出中的脏半对身份按无身份处理，避免向插件传播不可用主键。
        """
        normalized_id = str(self.media_id).strip() if self.media_id is not None else ""
        if not self.media_source or not normalized_id or normalized_id == "0":
            object.__setattr__(self, "media_source", None)
            object.__setattr__(self, "media_id", None)
        else:
            object.__setattr__(self, "media_id", normalized_id)
        return self


class SubscriptionSnapshot(_QuerySnapshot):
    """
    当前订阅的稳定只读快照，不包含 ORM 或写入行为。
    """

    id: int = Field(description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    type: Optional[str] = Field(default=None, description="类型标识")
    keyword: Optional[str] = Field(default=None, description="搜索关键字")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    season: Optional[int] = Field(default=None, description="季号")
    poster: Optional[str] = Field(default=None, description="海报")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    vote: Optional[float] = Field(default=None, description="评分")
    description: Optional[str] = Field(default=None, description="描述")
    filter: Optional[str] = Field(default=None, description="宿主保存的资源过滤规则")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    quality: Optional[str] = Field(default=None, description="质量")
    resolution: Optional[str] = Field(default=None, description="分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    audio_quality: Optional[str] = Field(default=None, description="音质等级")
    audio_format: Optional[str] = Field(default=None, description="音频编码格式")
    min_bitrate: Optional[int] = Field(default=None, description="最低码率（bps）")
    min_bit_depth: Optional[int] = Field(default=None, description="最低位深（bit）")
    min_sample_rate: Optional[int] = Field(default=None, description="最低采样率（Hz）")
    total_episode: Optional[int] = Field(default=None, description="订阅的目标总集数，由宿主维护")
    start_episode: Optional[int] = Field(default=None, description="开始集数")
    lack_episode: Optional[int] = Field(default=None, description="缺失集数")
    note: Optional[JsonData] = Field(default=None, description="附加信息")
    state: Optional[str] = Field(default=None, description="当前业务状态")
    last_update: Optional[str] = Field(default=None, description="最后更新时间")
    date: Optional[str] = Field(default=None, description="时间")
    username: Optional[str] = Field(default=None, description="用户名")
    sites: Optional[list[int]] = Field(default=None, description="站点列表")
    downloader: Optional[str] = Field(default=None, description="下载器")
    best_version: Optional[int] = Field(default=None, description="是否洗版")
    best_version_full: Optional[int] = Field(default=None, description="是否只洗全集整包")
    current_priority: Optional[int] = Field(default=None, description="当前优先级")
    current_audio_format: Optional[str] = Field(default=None, description="当前音乐版本格式")
    current_bitrate: Optional[int] = Field(default=None, description="当前音乐版本码率（bps）")
    current_bit_depth: Optional[int] = Field(default=None, description="当前音乐版本位深（bit）")
    current_sample_rate: Optional[int] = Field(default=None, description="当前音乐版本采样率（Hz）")
    episode_priority: Optional[dict[str, int]] = Field(default=None, description="洗版时已下载剧集的优先级状态")
    save_path: Optional[str] = Field(default=None, description="保存路径")
    search_imdbid: Optional[int] = Field(default=None, description="是否使用 imdbid 搜索")
    manual_total_episode: Optional[int] = Field(default=None, description="手动指定的总集数")
    custom_words: Optional[str] = Field(default=None, description="自定义识别词")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    filter_groups: Optional[list[str]] = Field(default=None, description="过滤规则组")
    episode_group: Optional[str] = Field(default=None, description="剧集组")


class SubscriptionHistorySnapshot(_QuerySnapshot):
    """
    订阅完成历史的稳定只读快照。
    """

    id: int = Field(description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    type: Optional[str] = Field(default=None, description="类型标识")
    keyword: Optional[str] = Field(default=None, description="搜索关键字")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    season: Optional[int] = Field(default=None, description="季号")
    poster: Optional[str] = Field(default=None, description="海报")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    vote: Optional[float] = Field(default=None, description="评分")
    description: Optional[str] = Field(default=None, description="描述")
    filter: Optional[str] = Field(default=None, description="资源过滤规则")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    quality: Optional[str] = Field(default=None, description="质量")
    resolution: Optional[str] = Field(default=None, description="分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    audio_quality: Optional[str] = Field(default=None, description="音质等级")
    audio_format: Optional[str] = Field(default=None, description="音频编码格式")
    min_bitrate: Optional[int] = Field(default=None, description="最低码率（bps）")
    min_bit_depth: Optional[int] = Field(default=None, description="最低位深（bit）")
    min_sample_rate: Optional[int] = Field(default=None, description="最低采样率（Hz）")
    total_episode: Optional[int] = Field(default=None, description="总集数")
    start_episode: Optional[int] = Field(default=None, description="开始集数")
    date: Optional[str] = Field(default=None, description="时间")
    username: Optional[str] = Field(default=None, description="用户名")
    sites: Optional[list[int]] = Field(default=None, description="站点列表")
    best_version: Optional[int] = Field(default=None, description="是否洗版")
    best_version_full: Optional[int] = Field(default=None, description="是否只洗全集整包")
    current_priority: Optional[int] = Field(default=None, description="当前优先级")
    current_audio_format: Optional[str] = Field(default=None, description="当前音乐版本格式")
    current_bitrate: Optional[int] = Field(default=None, description="当前音乐版本码率（bps）")
    current_bit_depth: Optional[int] = Field(default=None, description="当前音乐版本位深（bit）")
    current_sample_rate: Optional[int] = Field(default=None, description="当前音乐版本采样率（Hz）")
    episode_priority: Optional[dict[str, int]] = Field(default=None, description="洗版时已下载剧集的优先级状态")
    save_path: Optional[str] = Field(default=None, description="保存路径")
    search_imdbid: Optional[int] = Field(default=None, description="是否使用 imdbid 搜索")
    custom_words: Optional[str] = Field(default=None, description="自定义识别词")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    classification_rule_id: Optional[str] = Field(default=None, description="命中的分类规则标识")
    classification_policy_revision: Optional[int] = Field(default=None, description="执行分类时的策略版本")
    classification_source: Optional[str] = Field(default=None, description="最终采用的分类来源")
    filter_groups: Optional[list[str]] = Field(default=None, description="过滤规则组")
    episode_group: Optional[str] = Field(default=None, description="剧集组")


class DownloadHistorySnapshot(_QuerySnapshot):
    """
    下载历史的稳定只读快照。
    """

    id: int = Field(description="记录标识")
    path: Optional[str] = Field(default=None, description="文件或目录路径")
    type: Optional[str] = Field(default=None, description="类型标识")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    seasons: Optional[str] = Field(default=None, description="季季集清单")
    episodes: Optional[str] = Field(default=None, description="剧集信息")
    image: Optional[str] = Field(default=None, description="图片")
    poster: Optional[str] = Field(default=None, description="海报")
    downloader: Optional[str] = Field(default=None, description="下载器")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")
    torrent_name: Optional[str] = Field(default=None, description="种子名称")
    torrent_description: Optional[str] = Field(default=None, description="种子描述")
    torrent_site: Optional[str] = Field(default=None, description="站点")
    userid: Optional[str] = Field(default=None, description="用户 ID")
    username: Optional[str] = Field(default=None, description="用户名")
    channel: Optional[str] = Field(default=None, description="消息渠道标识")
    date: Optional[str] = Field(default=None, description="时间")
    note: Optional[JsonData] = Field(default=None, description="附加信息")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    classification_rule_id: Optional[str] = Field(default=None, description="命中的分类规则标识")
    classification_policy_revision: Optional[int] = Field(default=None, description="执行分类时的策略版本")
    classification_source: Optional[str] = Field(default=None, description="最终采用的分类来源")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    custom_words: Optional[str] = Field(default=None, description="自定义识别词")


class TransferHistorySnapshot(_QuerySnapshot):
    """
    整理历史的稳定只读快照；内部任务结算标识不会暴露给插件。
    """

    id: int = Field(description="记录标识")
    src: Optional[str] = Field(default=None, description="关联源路径")
    src_storage: Optional[str] = Field(default=None, description="源存储类型")
    src_fileitem: Optional[JsonData] = Field(default=None, description="源文件项（含文件大小等运行时字段，与 to_dict 直出保持一致）")
    dest: Optional[str] = Field(default=None, description="目的目录")
    dest_storage: Optional[str] = Field(default=None, description="目标存储类型")
    dest_fileitem: Optional[JsonData] = Field(default=None, description="目标文件项")
    mode: Optional[str] = Field(default=None, description="操作模式")
    type: Optional[str] = Field(default=None, description="类型标识")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    category: Optional[str] = Field(default=None, description="媒体分类")
    classification_rule_id: Optional[str] = Field(default=None, description="命中的分类规则标识")
    classification_policy_revision: Optional[int] = Field(default=None, description="执行分类时的策略版本")
    classification_source: Optional[str] = Field(default=None, description="最终采用的分类来源")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    audio_format: Optional[str] = Field(default=None, description="音频编码格式")
    audio_lossless: Optional[bool] = Field(default=None, description="是否无损音频")
    bit_depth: Optional[int] = Field(default=None, description="实际位深（bit）")
    sample_rate: Optional[int] = Field(default=None, description="实际采样率（Hz）")
    bitrate: Optional[int] = Field(default=None, description="实际码率（bps）")
    seasons: Optional[str] = Field(default=None, description="季季集清单")
    episodes: Optional[str] = Field(default=None, description="剧集信息")
    image: Optional[str] = Field(default=None, description="图片")
    downloader: Optional[str] = Field(default=None, description="下载器")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")
    status: bool = Field(default=False, description="整理是否成功，旧记录的空状态按失败处理")
    errmsg: Optional[str] = Field(default=None, description="失败原因")
    date: Optional[str] = Field(default=None, description="时间")
    files: Optional[JsonData] = Field(default=None, description="文件清单")
    episode_group: Optional[str] = Field(default=None, description="剧集组")

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> bool:
        """
        旧记录的 NULL 状态按失败处理，避免错误地向插件声明整理成功。
        """
        return bool(value)


class SubscriptionFilter(MediaIdentityQuery):
    """
    当前订阅的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配，其余字段均精确匹配；
    ``music_type=recording`` 同时匹配未标注音乐类型的旧单曲记录。
    """

    ids: tuple[int, ...] = Field(default=(), description="按记录标识筛选，为空时不限制")
    names: tuple[str, ...] = Field(default=(), description="别名和译名")
    states: tuple[str, ...] = Field(default=(), description="按订阅状态筛选，为空时不限制")
    usernames: tuple[str, ...] = Field(default=(), description="按用户名筛选，为空时不限制")
    media_types: tuple[MediaType, ...] = Field(default=(), description="按媒体类型筛选，为空时不限制")
    season: Optional[int] = Field(default=None, description="季号")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")


class SubscriptionHistoryFilter(MediaIdentityQuery):
    """
    订阅完成历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配，其余字段均精确匹配；
    ``music_type=recording`` 同时匹配未标注音乐类型的旧单曲记录。
    """

    ids: tuple[int, ...] = Field(default=(), description="按记录标识筛选，为空时不限制")
    names: tuple[str, ...] = Field(default=(), description="别名和译名")
    usernames: tuple[str, ...] = Field(default=(), description="按用户名筛选，为空时不限制")
    media_types: tuple[MediaType, ...] = Field(default=(), description="按媒体类型筛选，为空时不限制")
    season: Optional[int] = Field(default=None, description="季号")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")


class DownloadHistoryFilter(MediaIdentityQuery):
    """
    下载历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配；除 ``text`` 外的字符串
    字段均精确匹配。``text`` 对标题和路径执行转义后的字面包含查询，不把 ``%``
    或 ``_`` 解释为通配符。``music_type=recording`` 同时匹配旧 NULL 记录。
    """

    ids: tuple[int, ...] = Field(default=(), description="按记录标识筛选，为空时不限制")
    media_types: tuple[MediaType, ...] = Field(default=(), description="按媒体类型筛选，为空时不限制")
    title: Optional[str] = Field(default=None, description="标题")
    text: Optional[str] = Field(default=None, description="文本内容")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    seasons: Optional[str] = Field(default=None, description="季季集清单")
    episodes: Optional[str] = Field(default=None, description="剧集信息")
    path: Optional[str] = Field(default=None, description="文件或目录路径")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")
    username: Optional[str] = Field(default=None, description="用户名")
    usernames: tuple[str, ...] = Field(default=(), description="按用户名筛选，为空时不限制")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    episode_group: Optional[str] = Field(default=None, description="剧集组")


class TransferHistoryFilter(MediaIdentityQuery):
    """
    整理历史的组合筛选合同。

    所有非空字段按 AND 组合，tuple 字段内部按 IN 匹配；除 ``text`` 外的字符串
    字段均精确匹配。``text`` 对标题、源路径和目标路径执行转义后的字面包含查询。
    ``require_media_identity`` 只保留来源可解析且原生 ID 非空、非零的记录；
    ``status=False`` 包含旧 NULL 状态，``music_type=recording`` 包含旧 NULL 类型。
    """

    ids: tuple[int, ...] = Field(default=(), description="按记录标识筛选，为空时不限制")
    media_types: tuple[MediaType, ...] = Field(default=(), description="按媒体类型筛选，为空时不限制")
    media_sources: tuple[MediaSource, ...] = Field(default=(), description="按媒体数据源筛选，为空时不限制")
    require_media_identity: bool = Field(default=False, description="是否只保留来源和原生标识均有效的记录")
    title: Optional[str] = Field(default=None, description="标题")
    text: Optional[str] = Field(default=None, description="文本内容")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    seasons: Optional[str] = Field(default=None, description="季季集清单")
    episodes: Optional[str] = Field(default=None, description="剧集信息")
    src: Optional[str] = Field(default=None, description="关联源路径")
    dest: Optional[str] = Field(default=None, description="目的目录")
    status: Optional[bool] = Field(default=None, description="按整理成功与否筛选；省略或为空时不筛选，False 同时匹配旧空状态")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    episode_group: Optional[str] = Field(default=None, description="剧集组")


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
