"""
订阅、搜索执行进度与订阅分享接口的数据结构。
"""

import json
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MUSIC_ENTITY_ALBUM as _MUSIC_ENTITY_ALBUM
from app.schemas.types import MediaSource, MediaType


class SubscriptionActivityEvent(BaseModel):
    """
    面向用户的结构化业务活动，明细只包含安全资源摘要。
    """

    event_id: str = Field(description="活动事件标识")
    kind: str = Field(description="订阅业务活动类型")
    source: str = Field(description="触发该业务活动的入口或来源")
    status: str = Field(description="本次订阅业务轮次的运行或结束状态，独立于订阅是否启用")
    started_at: str = Field(description="开始时间")
    finished_at: Optional[str] = Field(default=None, description="结束时间")
    counts: Dict[str, int] = Field(default_factory=dict, description="活动事件涉及的分类计数")
    details: Dict[str, JsonData] = Field(default_factory=dict, description="活动事件的详细数据")
    details_loaded: bool = Field(default=True, description="是否已加载活动详情")


class SubscriptionActivityPage(BaseModel):
    """
    业务活动游标分页；升级前的记录不会由运行日志补造。
    """

    items: List[SubscriptionActivityEvent] = Field(default_factory=list, description="当前页条目列表")
    next_cursor: Optional[str] = Field(default=None, description="下一页查询游标，为空时没有后续记录")


def compute_subscribe_completed_episode(subscribe: "Subscribe") -> Optional[int]:
    """
    计算订阅"已完成"集数派生值，仅用于响应填充，不入库。

    普通电视剧按 ``total_episode - lack_episode`` 计算；分集洗版按订阅目标范围内
    priority==100 的分集数量计算；全集洗版按整包准入基线是否达到 100 计算。

    :param subscribe: 需要计算进度的订阅快照
    :return: 已完成集数；不是电视剧或没有总集数时返回 None
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


def compute_subscribe_completed_tracks(subscribe: Any) -> Optional[int]:
    """
    计算专辑订阅已累计曲目数，仅用于响应派生，不把内部音轨键暴露给客户端。

    :param subscribe: 含下载曲目事实的订阅对象或字典
    :return: 去重后的已下载曲目数，不超过总曲目数；不满足专辑进度条件时返回 None
    """
    def value(name: str) -> Any:
        """
        从快照、ORM 行或字典读取订阅字段。
        """
        if isinstance(subscribe, dict):
            return subscribe.get(name)
        return getattr(subscribe, name, None)

    if isinstance(subscribe, dict):
        has_track_facts = "downloaded_tracks" in subscribe
    else:
        has_track_facts = hasattr(subscribe, "downloaded_tracks")
    if not has_track_facts:
        return None

    if value("type") != MediaType.MUSIC.value or value("music_type") != _MUSIC_ENTITY_ALBUM:
        return None
    try:
        total_tracks = int(value("total_tracks") or 0)
    except (TypeError, ValueError):
        return None
    if total_tracks <= 0:
        return None
    downloaded_tracks = value("downloaded_tracks") or []
    if not isinstance(downloaded_tracks, (list, tuple, set, frozenset)):
        return 0
    completed_tracks = len({item for item in downloaded_tracks if isinstance(item, str) and item})
    return min(completed_tracks, total_tracks)


class SubscriptionExecutionStatus(BaseModel):
    """
    订阅列表可见的当前业务执行状态。
    """

    state: str = Field(description="当前业务状态")
    phase: str = Field(description="当前执行阶段")
    updated_at: str = Field(description="更新时间")
    source: Optional[str] = Field(default=None, description="触发本轮订阅执行的入口或来源")
    batch_id: Optional[str] = Field(default=None, description="所属批次标识")
    task_id: Optional[str] = Field(default=None, description="任务标识")
    current_site_id: Optional[int] = Field(default=None, description="正在处理的站点标识")
    next_run_at: Optional[str] = Field(default=None, description="下次计划执行时间")
    error: Optional[str] = Field(default=None, description="错误摘要")
    can_cancel: bool = Field(default=False, description="当前任务是否允许取消")

    model_config = ConfigDict(from_attributes=True)


class SubscriptionBatchStatus(BaseModel):
    """
    订阅搜索批次的用户可见进度和操作能力。
    """

    batch_id: str = Field(description="所属批次标识")
    source: str = Field(description="触发搜索批次的入口或来源")
    state: str = Field(description="当前业务状态")
    phase: str = Field(description="当前执行阶段")
    total_count: int = Field(description="总条目数量")
    processed_count: int = Field(description="已处理的任务数量")
    finished_count: int = Field(description="已结束的任务数量")
    failed_count: int = Field(description="执行失败的任务数量")
    cancelled_count: int = Field(description="已取消的任务数量")
    created_at: str = Field(description="创建时间")
    updated_at: str = Field(description="更新时间")
    download_counts: Dict[str, int] = Field(default_factory=dict, description="成功下载任务分类计数：submitted 总数、initial_submitted 首次订阅、upgrade_submitted 洗版（含混合整包）、full_upgrade_submitted 全集洗版子集；旧活动不推断来源")
    downloads: List[Dict[str, JsonData]] = Field(default_factory=list, description="成功下载的安全摘要，含 subscription_download 提交类型与全集洗版模式；最多 100 条")
    downloads_omitted: int = Field(default=0, description="因活动或批次明细上限省略的成功下载数量，计数不受此上限影响")
    skipped_count: int = Field(default=0, description="已跳过的订阅任务数量")
    current_subscription_id: Optional[int] = Field(default=None, description="正在处理的订阅标识")
    current_site_id: Optional[int] = Field(default=None, description="正在处理的站点标识")
    error: Optional[str] = Field(default=None, description="错误摘要")
    can_cancel: bool = Field(default=False, description="当前任务是否允许取消")

    model_config = ConfigDict(from_attributes=True)


class SubscriptionSearchSubmission(BaseModel):
    """
    手工订阅搜索已安排后的轻量跟踪信息。
    """

    batch_id: Optional[str] = Field(default=None, description="所属批次标识")
    batch_ids: List[str] = Field(default_factory=list, description="本次提交涉及的批次标识列表")
    target_count: int = Field(default=0, description="本次搜索涉及的订阅数量")
    queued_count: int = Field(default=0, description="本次新增排队任务数量")
    ongoing_count: int = Field(default=0, description="已在运行且无需重复提交的任务数量")
    single: bool = Field(default=False, description="本次是否为单条订阅搜索")


class Subscribe(OptionalMediaIdentityMixin, BaseModel):
    """
    订阅输入与响应模型，媒体身份必须为空对或完整有效对。

    :cvar CLEARABLE_OPTIONAL_FIELDS: 表单显式清空时保留为 None 的可选设置，用于覆盖旧值
    :cvar PUBLIC_WRITE_EXCLUDED_FIELDS: 公共创建和更新接口禁止写入的系统字段与运行事实
    """

    CLEARABLE_OPTIONAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "filter", "include", "exclude", "quality", "resolution", "effect",
        "audio_quality", "audio_format",
        "custom_words", "keyword", "save_path", "episode_group", "downloader",
        "min_bitrate", "min_bit_depth", "min_sample_rate",
        "media_category_id", "media_category",
    })

    PUBLIC_WRITE_EXCLUDED_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "id", "poster", "backdrop", "vote", "description", "lack_episode", "completed_episode", "completed_tracks",
        "note", "state", "last_update", "username", "current_priority", "episode_priority", "date",
        "current_audio_format", "current_bitrate", "current_bit_depth", "current_sample_rate",
        "classification_rule_id", "classification_policy_revision", "classification_source",
        "execution_status", "last_search",
    })

    id: Optional[int] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="订阅名称")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    type: Optional[str] = Field(default=None, description="订阅类型 电影/电视剧")
    search_interval: Optional[int] = Field(default=None, ge=1, le=8760, description="定时搜索间隔（小时）；空值跟随系统设置")
    last_search: Optional[str] = Field(default=None, description="最近一次主动搜索开始时间，仅供读取")
    keyword: Optional[str] = Field(default=None, description="搜索关键字")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    completed_tracks: Optional[int] = Field(default=None, description="已累计下载曲目数，仅由后端根据专辑音轨事实派生")
    season: Optional[int] = Field(default=None, description="季号")
    poster: Optional[str] = Field(default=None, description="海报")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    vote: Optional[float] = Field(default=0.0, description="评分")
    description: Optional[str] = Field(default=None, description="描述")
    filter: Optional[str] = Field(default=None, description="过滤规则")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    quality: Optional[str] = Field(default=None, description="质量")
    resolution: Optional[str] = Field(default=None, description="分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    audio_quality: Optional[str] = Field(default=None, description="音乐音质等级，可用 | 组合 hires/lossless/lossy")
    audio_format: Optional[str] = Field(default=None, description="音频格式正则，如 FLAC|ALAC")
    min_bitrate: Optional[int] = Field(default=None, description="最低码率（bps）")
    min_bit_depth: Optional[int] = Field(default=None, description="最低位深（bit）")
    min_sample_rate: Optional[int] = Field(default=None, description="最低采样率（Hz）")
    total_episode: Optional[int] = Field(default=0, description="总集数")
    start_episode: Optional[int] = Field(default=0, description="开始集数")
    lack_episode: Optional[int] = Field(default=0, description="缺失集数")
    completed_episode: Optional[int] = Field(default=None, description="已完成集数")
    note: Optional[List[int]] = Field(default=None, description="附加信息")
    state: Optional[str] = Field(default=None, description="状态：N-新建， R-订阅中")
    last_update: Optional[str] = Field(default=None, description="最后更新时间")
    username: Optional[str] = Field(default=None, description="订阅用户")
    sites: Optional[List[int]] = Field(default_factory=list, description="订阅站点")
    downloader: Optional[str] = Field(default=None, description="下载器")
    best_version: Optional[int] = Field(default=None, description="是否洗版")
    best_version_full: Optional[int] = Field(default=None, description="是否只洗全集整包")
    current_priority: Optional[int] = Field(default=None, description="当前优先级")
    current_audio_format: Optional[str] = Field(default=None, description="当前音乐版本格式")
    current_bitrate: Optional[int] = Field(default=None, description="当前音乐版本码率（bps）")
    current_bit_depth: Optional[int] = Field(default=None, description="当前音乐版本位深（bit）")
    current_sample_rate: Optional[int] = Field(default=None, description="当前音乐版本采样率（Hz）")
    episode_priority: Optional[Dict[str, int]] = Field(default=None, description="洗版时已下载剧集的优先级状态")
    save_path: Optional[str] = Field(default=None, description="保存路径")
    search_imdbid: Optional[int] = Field(default=0, description="是否使用 imdbid 搜索")
    date: Optional[str] = Field(default=None, description="时间")
    custom_words: Optional[str] = Field(default=None, description="自定义识别词")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    classification_rule_id: Optional[str] = Field(default=None, description="历史命中的分类规则标识，活动订阅中为空")
    classification_policy_revision: Optional[int] = Field(default=None, description="历史执行时分类策略版本，活动订阅中为空")
    classification_source: Optional[str] = Field(default=None, description="历史最终分类来源，活动订阅中为空")
    filter_groups: Optional[List[str]] = Field(default_factory=list, description="过滤规则组")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    execution_status: Optional[SubscriptionExecutionStatus] = Field(default=None, description="当前搜索或下载执行状态，只用于响应投影")

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
        的可选设置、搜索周期保留为 None，以便更新接口区分“未提交”与“显式清空”。
        """
        if isinstance(data, dict):
            data = dict(data)
            for key, value in list(data.items()):
                if isinstance(value, str) and value == "":
                    if key in {"media_source", "media_id", "search_interval"} or key in cls.CLEARABLE_OPTIONAL_FIELDS:
                        data[key] = None
                    else:
                        data.pop(key)
        return data

    @model_validator(mode="after")
    def _fill_completed_episode(self) -> "Subscribe":
        """
        填充 ``completed_episode`` 派生字段。电视剧订阅按 best_version 分支计算，。
        电影或缺少 total_episode 时保持 None。
        """
        if self.completed_episode is not None:
            # 调用方显式提供过的值不覆盖
            return self
        self.completed_episode = compute_subscribe_completed_episode(self)
        return self

    def to_public_write_payload(self, *, exclude_unset: bool = False) -> Dict[str, Any]:
        """
        裁剪公共订阅写入字段，可仅保留更新请求显式提交的字段。

        文本分类表单会带回详情中的空稳定 ID；同时提交非空路径时，按旧路径输入处理，
        避免分类规范化把新输入误判为清空。路径和 ID 都为空时保留显式清空语义。
        """
        payload = self.model_dump(
            exclude=set(self.PUBLIC_WRITE_EXCLUDED_FIELDS),
            exclude_unset=exclude_unset,
        )
        if payload.get("media_category") and not str(payload.get("media_category_id") or "").strip():
            payload.pop("media_category_id", None)
        return payload


class SubscribeShare(OptionalMediaIdentityMixin, BaseModel):
    """
    可供其他用户复用的订阅分享信息。
    """

    id: Optional[int] = Field(default=None, description="分享ID")
    subscribe_id: Optional[int] = Field(default=None, description="订阅ID")
    share_title: Optional[str] = Field(default=None, description="分享标题")
    share_comment: Optional[str] = Field(default=None, description="分享说明")
    share_user: Optional[str] = Field(default=None, description="分享人")
    share_uid: Optional[str] = Field(default=None, description="分享人唯一ID")
    name: Optional[str] = Field(default=None, description="订阅名称")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    type: Optional[str] = Field(default=None, description="订阅类型 电影/电视剧")
    keyword: Optional[str] = Field(default=None, description="搜索关键字")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    season: Optional[int] = Field(default=None, description="季号")
    poster: Optional[str] = Field(default=None, description="海报")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    vote: Optional[float] = Field(default=0.0, description="评分")
    description: Optional[str] = Field(default=None, description="描述")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    quality: Optional[str] = Field(default=None, description="质量")
    resolution: Optional[str] = Field(default=None, description="分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    audio_quality: Optional[str] = Field(default=None, description="音乐音质等级")
    audio_format: Optional[str] = Field(default=None, description="音频格式")
    min_bitrate: Optional[int] = Field(default=None, description="最低码率（bps）")
    min_bit_depth: Optional[int] = Field(default=None, description="最低位深（bit）")
    min_sample_rate: Optional[int] = Field(default=None, description="最低采样率（Hz）")
    total_episode: Optional[int] = Field(default=0, description="总集数")
    date: Optional[str] = Field(default=None, description="时间")
    custom_words: Optional[str] = Field(default=None, description="自定义识别词")
    media_category_id: Optional[str] = Field(default=None, description="自定义媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    episode_group: Optional[str] = Field(default=None, description="自定义剧集组")
    count: Optional[int] = Field(default=0, description="复用人次")


class SubscribeShareStatistics(BaseModel):
    """
    单个用户的订阅分享数量与复用统计。
    """

    share_user: Optional[str] = Field(default=None, description="分享人")
    share_count: Optional[int] = Field(default=0, description="分享数量")
    total_reuse_count: Optional[int] = Field(default=0, description="总复用人次")


class SubscribeDownloadFileInfo(BaseModel):
    """
    订阅剧集关联的下载文件信息。
    """

    torrent_title: Optional[str] = Field(default=None, description="种子名称")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    downloader: Optional[str] = Field(default=None, description="下载器")
    hash: Optional[str] = Field(default=None, description="下载任务 hash")
    file_path: Optional[str] = Field(default=None, description="文件路径")


class SubscribeLibraryFileInfo(BaseModel):
    """
    订阅剧集关联的媒体库文件信息。
    """

    storage: Optional[str] = Field(default="local", description="存储")
    file_path: Optional[str] = Field(default=None, description="文件路径")
    server: Optional[str] = Field(default=None, description="媒体服务器名称")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型：emby、jellyfin、plex 等")
    itemid: Optional[str] = Field(default=None, description="媒体服务器条目 ID")


class SubscribeEpisodeInfo(BaseModel):
    """
    订阅单集的元数据及关联文件。
    """

    title: Optional[str] = Field(default=None, description="标题")
    description: Optional[str] = Field(default=None, description="描述")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    download: Optional[List[SubscribeDownloadFileInfo]] = Field(default_factory=list, description="下载文件信息")
    library: Optional[List[SubscribeLibraryFileInfo]] = Field(default_factory=list, description="媒体库文件信息")


class SubscrbieInfo(BaseModel):
    """
    订阅详情及按集号归组的文件信息。
    """

    subscribe: Optional[Subscribe] = Field(default=None, description="订阅信息")
    episodes: Optional[Dict[int, SubscribeEpisodeInfo]] = Field(
        default_factory=dict,
        description="集信息 {集号: {download: 文件路径，library: 文件路径, backdrop: url, title: 标题, description: 描述}}",
    )


class SubscribeDeletionResult(BaseModel):
    """
    订阅删除成功后的机器可判断结果。
    """

    status: Literal["deleted"] = Field(description="deleted 表示数据库事务已提交；不存在或无权限由 HTTP 状态码表达")
