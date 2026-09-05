import json
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource, MediaType


def compute_subscribe_completed_episode(subscribe: "Subscribe") -> Optional[int]:
    """
    计算订阅"已完成"集数派生值，仅用于响应填充，不入库。

    普通电视剧按 ``total_episode - lack_episode`` 计算；分集洗版按订阅目标范围内
    priority==100 的分集数量计算；全集洗版按整包准入基线是否达到 100 计算。
    """
    total_episode = subscribe.total_episode or 0
    if subscribe.type != MediaType.TV.value or not total_episode:
        return None

    start_episode = subscribe.start_episode or 1
    if not subscribe.best_version:
        lack = subscribe.lack_episode or 0
        return max(total_episode - lack, 0)

    if subscribe.best_version_full:
        completed_targets = max(total_episode - start_episode + 1, 0) \
            if subscribe.current_priority == 100 else 0
        return min(min(max(start_episode - 1, 0), total_episode) + completed_targets, total_episode)

    episode_priority = subscribe.episode_priority or {}
    if not episode_priority and subscribe.current_priority is not None:
        # 兼容只有整体优先级的洗版快照，响应派生值需与链路侧按集口径保持一致。
        episode_priority = {
            str(episode): int(subscribe.current_priority)
            for episode in range(start_episode, total_episode + 1)
        }
    priority_completed = sum(
        1
        for ep_key, priority in episode_priority.items()
        if str(ep_key).isdigit()
        and start_episode <= int(ep_key) <= total_episode
        and priority == 100
    )
    return min(max(start_episode - 1, 0), total_episode) + priority_completed


class SubscriptionExecutionStatus(BaseModel):  # type: ignore[misc]
    """订阅列表可见的当前业务执行状态。"""

    state: str
    phase: str
    updated_at: str
    source: Optional[str] = None
    batch_id: Optional[str] = None
    task_id: Optional[str] = None
    current_site_id: Optional[int] = None
    next_run_at: Optional[str] = None
    error: Optional[str] = None
    can_cancel: bool = False

    model_config = ConfigDict(from_attributes=True)


class SubscriptionBatchStatus(BaseModel):  # type: ignore[misc]
    """订阅搜索批次的用户可见进度和操作能力。"""

    batch_id: str
    source: str
    state: str
    phase: str
    total_count: int
    processed_count: int
    finished_count: int
    failed_count: int
    cancelled_count: int
    created_at: str
    updated_at: str
    skipped_count: int = 0
    current_subscription_id: Optional[int] = None
    current_site_id: Optional[int] = None
    error: Optional[str] = None
    can_cancel: bool = False

    model_config = ConfigDict(from_attributes=True)


class SubscriptionSearchSubmission(BaseModel):  # type: ignore[misc]
    """手工订阅搜索已安排后的轻量跟踪信息。"""

    batch_id: Optional[str] = None
    batch_ids: List[str] = Field(default_factory=list)
    target_count: int = 0
    queued_count: int = 0
    ongoing_count: int = 0
    single: bool = False


class Subscribe(OptionalMediaIdentityMixin, BaseModel):
    """订阅输入与响应模型，媒体身份必须为空对或完整有效对。"""

    # 表单用空字符串表达“全部”时必须保留显式清空语义，更新接口才能覆盖存量规则。
    CLEARABLE_FILTER_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "filter", "include", "exclude", "quality", "resolution", "effect",
        "audio_quality", "audio_format",
    })

    # 公共创建和更新接口不得接收系统字段和运行事实；其余字段默认作为订阅输入透传。
    PUBLIC_WRITE_EXCLUDED_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "id", "poster", "backdrop", "vote", "description", "lack_episode", "completed_episode",
        "note", "state", "last_update", "username", "current_priority", "episode_priority", "date",
        "current_audio_format", "current_bitrate", "current_bit_depth", "current_sample_rate",
        "classification_rule_id", "classification_policy_revision", "classification_source",
        "execution_status",
    })

    id: Optional[int] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 专辑预期总曲目数
    total_tracks: Optional[int] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 过滤规则
    filter: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 音乐音质等级，可用 | 组合 hires/lossless/lossy
    audio_quality: Optional[str] = None
    # 音频格式正则，如 FLAC|ALAC
    audio_format: Optional[str] = None
    # 最低码率（bps）
    min_bitrate: Optional[int] = None
    # 最低位深（bit）
    min_bit_depth: Optional[int] = None
    # 最低采样率（Hz）
    min_sample_rate: Optional[int] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集数
    start_episode: Optional[int] = 0
    # 缺失集数
    lack_episode: Optional[int] = 0
    # 已完成集数
    completed_episode: Optional[int] = None
    # 附加信息
    note: Optional[List[int]] = None
    # 状态：N-新建， R-订阅中
    state: Optional[str] = None
    # 最后更新时间
    last_update: Optional[str] = None
    # 订阅用户
    username: Optional[str] = None
    # 订阅站点
    sites: Optional[List[int]] = Field(default_factory=list)
    # 下载器
    downloader: Optional[str] = None
    # 是否洗版
    best_version: Optional[int] = None
    # 是否只洗全集整包
    best_version_full: Optional[int] = None
    # 当前优先级
    current_priority: Optional[int] = None
    # 当前音乐版本格式
    current_audio_format: Optional[str] = None
    # 当前音乐版本码率（bps）
    current_bitrate: Optional[int] = None
    # 当前音乐版本位深（bit）
    current_bit_depth: Optional[int] = None
    # 当前音乐版本采样率（Hz）
    current_sample_rate: Optional[int] = None
    # 洗版时已下载剧集的优先级状态
    episode_priority: Optional[Dict[str, int]] = None
    # 保存路径
    save_path: Optional[str] = None
    # 是否使用 imdbid 搜索
    search_imdbid: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别稳定标识
    media_category_id: Optional[str] = None
    # 自定义媒体类别兼容路径快照
    media_category: Optional[str] = None
    # 历史命中的分类规则标识，活动订阅中为空
    classification_rule_id: Optional[str] = None
    # 历史执行时分类策略版本，活动订阅中为空
    classification_policy_revision: Optional[int] = None
    # 历史最终分类来源，活动订阅中为空
    classification_source: Optional[str] = None
    # 过滤规则组
    filter_groups: Optional[List[str]] = Field(default_factory=list)
    # 剧集组
    episode_group: Optional[str] = None
    # 当前搜索或下载执行状态，只用于响应投影
    execution_status: Optional[SubscriptionExecutionStatus] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("note", mode="before")
    @classmethod
    def _normalize_legacy_note(cls, value: Any) -> Any:
        """
        兼容历史字符串型 note。

        2.0 时代旧代码对 JSON 列显式做了 ``json.dumps``，历史数据可能是一层
        或两层 JSON 编码的字符串（如 ``'[1, 2, 3]'``），不解析会触发响应
        校验 500；解析失败按空值处理，避免脏数据阻塞整个订阅列表接口。
        """
        if not isinstance(value, str):
            return value
        parsed = value
        while isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (TypeError, ValueError):
                return None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, int)]
        return None

    @model_validator(mode="before")
    @classmethod
    def _normalize_empty_strings(cls, data: Any) -> Any:
        """
        将前端清空输入框后残留的空字符串视为空值。

        音乐等媒体类型的 season、total_episode、episode_priority 等数值或容器字段
        在表单中常以空字符串提交，而 Pydantic 不会把空字符串自动转为 None，会直接抛出
        校验异常导致接口返回 422。这里把空字符串键移除，等价于该字段未提供，从而复用字段
        默认值（如 ``total_episode`` 回退为 0、``sites`` 回退为空列表）。媒体身份键以及可清空
        的筛选字段保留为 None，以便更新接口区分“未提交”与“显式清空”。
        """
        if isinstance(data, dict):
            data = dict(data)
            for key, value in list(data.items()):
                if isinstance(value, str) and value == "":
                    if key in {"media_source", "media_id"} or key in cls.CLEARABLE_FILTER_FIELDS:
                        data[key] = None
                    else:
                        data.pop(key)
        return data

    @model_validator(mode="after")
    def _fill_completed_episode(self) -> "Subscribe":
        """
        填充 ``completed_episode`` 派生字段。电视剧订阅按 best_version 分支计算，
        电影或缺少 total_episode 时保持 None。
        """
        if self.completed_episode is not None:
            # 调用方显式提供过的值不覆盖
            return self
        self.completed_episode = compute_subscribe_completed_episode(self)
        return self

    def to_public_write_payload(self, *, exclude_unset: bool = False) -> Dict[str, Any]:
        """裁剪公共订阅写入字段，可仅保留更新请求显式提交的字段。"""
        return self.model_dump(
            exclude=self.PUBLIC_WRITE_EXCLUDED_FIELDS,
            exclude_unset=exclude_unset,
        )


class SubscribeShare(OptionalMediaIdentityMixin, BaseModel):
    """可供其他用户复用的订阅分享信息。"""

    # 分享ID
    id: Optional[int] = None
    # 订阅ID
    subscribe_id: Optional[int] = None
    # 分享标题
    share_title: Optional[str] = None
    # 分享说明
    share_comment: Optional[str] = None
    # 分享人
    share_user: Optional[str] = None
    # 分享人唯一ID
    share_uid: Optional[str] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 专辑预期总曲目数
    total_tracks: Optional[int] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 音乐音质等级
    audio_quality: Optional[str] = None
    # 音频格式
    audio_format: Optional[str] = None
    # 最低码率（bps）
    min_bitrate: Optional[int] = None
    # 最低位深（bit）
    min_bit_depth: Optional[int] = None
    # 最低采样率（Hz）
    min_sample_rate: Optional[int] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别稳定标识
    media_category_id: Optional[str] = None
    # 自定义媒体类别兼容路径快照
    media_category: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None
    # 复用人次
    count: Optional[int] = 0


class SubscribeShareStatistics(BaseModel):
    """单个用户的订阅分享数量与复用统计。"""

    # 分享人
    share_user: Optional[str] = None
    # 分享数量
    share_count: Optional[int] = 0
    # 总复用人次
    total_reuse_count: Optional[int] = 0


class SubscribeDownloadFileInfo(BaseModel):
    """订阅剧集关联的下载文件信息。"""

    # 种子名称
    torrent_title: Optional[str] = None
    # 站点名称
    site_name: Optional[str] = None
    # 下载器
    downloader: Optional[str] = None
    # hash
    hash: Optional[str] = None
    # 文件路径
    file_path: Optional[str] = None


class SubscribeLibraryFileInfo(BaseModel):
    """订阅剧集关联的媒体库文件信息。"""

    # 存储
    storage: Optional[str] = "local"
    # 文件路径
    file_path: Optional[str] = None
    # 媒体服务器名称
    server: Optional[str] = None
    # 媒体服务器类型：emby、jellyfin、plex 等
    server_type: Optional[str] = None
    # 媒体服务器条目 ID
    itemid: Optional[str] = None


class SubscribeEpisodeInfo(BaseModel):
    """订阅单集的元数据及关联文件。"""

    # 标题
    title: Optional[str] = None
    # 描述
    description: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 下载文件信息
    download: Optional[List[SubscribeDownloadFileInfo]] = Field(default_factory=list)
    # 媒体库文件信息
    library: Optional[List[SubscribeLibraryFileInfo]] = Field(default_factory=list)


class SubscrbieInfo(BaseModel):
    """订阅详情及按集号归组的文件信息。"""

    # 订阅信息
    subscribe: Optional[Subscribe] = None
    # 集信息 {集号: {download: 文件路径，library: 文件路径, backdrop: url, title: 标题, description: 描述}}
    episodes: Optional[Dict[int, SubscribeEpisodeInfo]] = Field(default_factory=dict)


class SubscribeDeletionResult(BaseModel):  # type: ignore[misc]
    """订阅删除成功后的机器可判断结果。"""

    # deleted 表示数据库事务已提交；不存在或无权限由 HTTP 状态码表达。
    status: Literal["deleted"]
