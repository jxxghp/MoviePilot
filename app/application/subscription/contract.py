"""
订阅持久化快照、写入数据与端口合同。
"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from typing import NoReturn, Optional, Protocol, cast
from uuid import uuid4

from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.common import JsonData
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType


class SubscriptionIdentityConflictError(ValueError):
    """
    目标完整身份已属于另一订阅，当前修改必须回滚且不得继承其进度。
    """


_SUBSCRIPTION_FIELDS = frozenset(
    {
        "name",
        "year",
        "type",
        "keyword",
        "search_interval",
        "last_search",
        "media_source",
        "media_id",
        "music_type",
        "total_tracks",
        "downloaded_tracks",
        "season",
        "poster",
        "backdrop",
        "vote",
        "description",
        "filter",
        "include",
        "exclude",
        "quality",
        "resolution",
        "effect",
        "audio_quality",
        "audio_format",
        "min_bitrate",
        "min_bit_depth",
        "min_sample_rate",
        "total_episode",
        "start_episode",
        "lack_episode",
        "note",
        "state",
        "last_update",
        "date",
        "username",
        "sites",
        "downloader",
        "best_version",
        "best_version_full",
        "current_priority",
        "current_audio_format",
        "current_bitrate",
        "current_bit_depth",
        "current_sample_rate",
        "episode_priority",
        "save_path",
        "search_imdbid",
        "manual_total_episode",
        "custom_words",
        "media_category_id",
        "media_category",
        "filter_groups",
        "episode_group",
    }
)

_CLASSIFICATION_HISTORY_FIELDS = frozenset(
    {
        "classification_rule_id",
        "classification_policy_revision",
        "classification_source",
    }
)

_SUBSCRIPTION_HISTORY_FIELDS = (_SUBSCRIPTION_FIELDS - {
    "lack_episode",
    "note",
    "downloaded_tracks",
    "state",
    "last_update",
    "last_search",
    "downloader",
    "manual_total_episode",
}) | _CLASSIFICATION_HISTORY_FIELDS | {"activity_key"}


class _FrozenJsonDict(dict[str, JsonData]):
    """
    保留 JSON 字典读取语义并拒绝原地修改。
    """

    def _reject(self, *args: object, **kwargs: object) -> NoReturn:
        """
        拒绝修改订阅快照内的 JSON。
        """
        raise TypeError("订阅快照 JSON 不可修改")

    __setitem__ = _reject
    __delitem__ = _reject
    __ior__ = _reject
    clear = _reject
    pop = _reject
    popitem = _reject
    setdefault = _reject
    update = _reject


class _FrozenJsonList(list[JsonData]):
    """
    保留 JSON 数组读取语义并拒绝原地修改。
    """

    def _reject(self, *args: object, **kwargs: object) -> NoReturn:
        """
        拒绝修改订阅快照内的 JSON。
        """
        raise TypeError("订阅快照 JSON 不可修改")

    __setitem__ = _reject
    __delitem__ = _reject
    __iadd__ = _reject
    __imul__ = _reject
    append = _reject
    clear = _reject
    extend = _reject
    insert = _reject
    pop = _reject
    remove = _reject
    reverse = _reject
    sort = _reject


def _freeze_json(value: JsonData) -> JsonData:
    """
    递归复制并冻结 JSON 容器。
    """
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


def _copy_json(value: JsonData) -> JsonData:
    """
    把冻结 JSON 递归复制为普通容器。
    """
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """
    脱离 Session 的完整订阅快照。

    :param id: 活动订阅记录主键
    :param name: 订阅目标的媒体名称
    :param year: 媒体发行年份
    :param type: 媒体类型，区分电影、电视剧和音乐
    :param keyword: 搜索关键字
    :param search_interval: 定时搜索间隔（小时）；空值跟随系统设置
    :param media_source: 订阅媒体的标准数据来源，与 media_id 配对使用
    :param media_id: 数据来源中的原生媒体标识
    :param music_type: 音乐实体类型：recording 单曲、album 专辑
    :param total_tracks: 专辑预期总曲目数
    :param downloaded_tracks: 已接受资源中可识别音轨的稳定键，仅在活动订阅中累计
    :param season: 目标季号；不按季区分时为 None
    :param poster: 订阅展示的海报图片地址
    :param backdrop: 订阅展示的背景图片地址
    :param vote: 来源提供的媒体评分
    :param description: 媒体内容简介
    :param filter: 订阅过滤规则配置
    :param include: 资源标题必须匹配的包含规则
    :param exclude: 资源标题命中即拒绝的排除规则
    :param quality: 允许的资源质量或发行类型规则
    :param resolution: 允许的资源分辨率规则
    :param effect: 允许的画面特效规则
    :param audio_quality: 音乐音质等级，可用 | 组合 hires/lossless/lossy
    :param audio_format: 音频格式正则，如 FLAC|ALAC
    :param min_bitrate: 最低码率（bps）
    :param min_bit_depth: 最低位深（bit）
    :param min_sample_rate: 最低采样率（Hz）
    :param total_episode: 总集数
    :param start_episode: 开始集数
    :param lack_episode: 缺失集数
    :param note: 订阅下载进度记录的集号列表
    :param state: 订阅状态：N 为新建、R 为订阅中、P 为待定、S 为暂停
    :param last_update: 最后更新时间
    :param last_search: 最近一次主动搜索开始时间，仅供读取
    :param date: 订阅创建时的时间文本
    :param username: 订阅所属的本地用户名；None 表示未绑定用户
    :param sites: 允许搜索和匹配的站点标识列表
    :param downloader: 下载所使用的下载器名称
    :param best_version: 是否启用更高质量版本替换
    :param best_version_full: 洗版时是否只接受全集整包
    :param current_priority: 当前已接受版本的优先级基线
    :param current_audio_format: 当前音乐版本格式
    :param current_bitrate: 当前音乐版本码率（bps）
    :param current_bit_depth: 当前音乐版本位深（bit）
    :param current_sample_rate: 当前音乐版本采样率（Hz）
    :param episode_priority: 洗版时已下载剧集的优先级状态
    :param save_path: 订阅指定的下载保存路径
    :param search_imdbid: 是否使用 imdbid 搜索
    :param manual_total_episode: 总集数是否由用户手工指定，避免自动元数据刷新覆盖
    :param custom_words: 自定义识别词
    :param media_category_id: 自定义媒体类别稳定标识
    :param media_category: 自定义媒体类别兼容路径快照
    :param filter_groups: 订阅选用的过滤规则组名称列表
    :param episode_group: 剧集组标识；未指定时使用来源默认剧集编排
    :param activity_key: 贯穿活动订阅与历史订阅的业务活动归属键
    """

    id: int
    name: str
    year: Optional[str] = None
    type: Optional[str] = None
    keyword: Optional[str] = None
    search_interval: Optional[int] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    downloaded_tracks: Optional[builtins.list[str]] = None
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
    lack_episode: Optional[int] = None
    note: Optional[builtins.list[int]] = None
    state: str = "N"
    last_update: Optional[str] = None
    last_search: Optional[str] = None
    date: Optional[str] = None
    username: Optional[str] = None
    sites: Optional[builtins.list[int]] = None
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
    filter_groups: Optional[builtins.list[str]] = None
    episode_group: Optional[str] = None
    activity_key: Optional[str] = None

    def __post_init__(self) -> None:
        """
        冻结订阅中的全部 JSON 列。
        """
        for name in ("note", "downloaded_tracks", "sites", "episode_priority", "filter_groups"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))

    def to_dict(self) -> dict[str, JsonData]:
        """
        返回事件和响应可安全修改的独立字典。
        """
        return {field.name: _copy_json(cast(JsonData, getattr(self, field.name))) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SubscriptionHistorySnapshot:
    """
    脱离 Session 的完整订阅历史快照。

    :param id: 已完成订阅的历史记录主键
    :param name: 订阅目标的媒体名称
    :param year: 媒体发行年份
    :param type: 媒体类型，区分电影、电视剧和音乐
    :param keyword: 搜索关键字
    :param search_interval: 定时搜索间隔（小时）；空值跟随系统设置
    :param media_source: 订阅媒体的标准数据来源，与 media_id 配对使用
    :param media_id: 数据来源中的原生媒体标识
    :param music_type: 音乐实体类型：recording 单曲、album 专辑
    :param total_tracks: 专辑预期总曲目数
    :param season: 目标季号；不按季区分时为 None
    :param poster: 订阅展示的海报图片地址
    :param backdrop: 订阅展示的背景图片地址
    :param vote: 来源提供的媒体评分
    :param description: 媒体内容简介
    :param filter: 订阅过滤规则配置
    :param include: 资源标题必须匹配的包含规则
    :param exclude: 资源标题命中即拒绝的排除规则
    :param quality: 允许的资源质量或发行类型规则
    :param resolution: 允许的资源分辨率规则
    :param effect: 允许的画面特效规则
    :param audio_quality: 音乐音质等级，可用 | 组合 hires/lossless/lossy
    :param audio_format: 音频格式正则，如 FLAC|ALAC
    :param min_bitrate: 最低码率（bps）
    :param min_bit_depth: 最低位深（bit）
    :param min_sample_rate: 最低采样率（Hz）
    :param total_episode: 总集数
    :param start_episode: 开始集数
    :param date: 订阅创建时的时间文本
    :param username: 订阅所属的本地用户名；None 表示未绑定用户
    :param sites: 允许搜索和匹配的站点标识列表
    :param best_version: 是否启用更高质量版本替换
    :param best_version_full: 洗版时是否只接受全集整包
    :param current_priority: 当前已接受版本的优先级基线
    :param current_audio_format: 当前音乐版本格式
    :param current_bitrate: 当前音乐版本码率（bps）
    :param current_bit_depth: 当前音乐版本位深（bit）
    :param current_sample_rate: 当前音乐版本采样率（Hz）
    :param episode_priority: 洗版时已下载剧集的优先级状态
    :param save_path: 订阅指定的下载保存路径
    :param search_imdbid: 是否使用 imdbid 搜索
    :param custom_words: 自定义识别词
    :param media_category_id: 自定义媒体类别稳定标识
    :param media_category: 自定义媒体类别兼容路径快照
    :param classification_rule_id: 历史命中的分类规则标识，活动订阅中为空
    :param classification_policy_revision: 历史执行时分类策略版本，活动订阅中为空
    :param classification_source: 历史最终分类来源，活动订阅中为空
    :param filter_groups: 订阅选用的过滤规则组名称列表
    :param episode_group: 剧集组标识；未指定时使用来源默认剧集编排
    :param activity_key: 贯穿活动订阅与历史订阅的业务活动归属键
    """

    id: int
    name: str
    year: Optional[str] = None
    type: Optional[str] = None
    keyword: Optional[str] = None
    search_interval: Optional[int] = None
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
    sites: Optional[builtins.list[int]] = None
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
    filter_groups: Optional[builtins.list[str]] = None
    episode_group: Optional[str] = None
    activity_key: Optional[str] = None

    def __post_init__(self) -> None:
        """
        冻结历史中的全部 JSON 列。
        """
        for name in ("sites", "episode_priority", "filter_groups"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))

    def to_dict(self) -> dict[str, JsonData]:
        """
        返回事件和响应可安全修改的独立字典。
        """
        return {field.name: _copy_json(cast(JsonData, getattr(self, field.name))) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SubscriptionIdentity:
    """
    订阅去重与来源定位使用的明确媒体身份。

    :param media_source: 订阅媒体的标准数据来源，与 media_id 配对使用
    :param media_id: 数据来源中的原生媒体标识
    :param type: 媒体类型，区分电影、电视剧和音乐
    :param season: 目标季号；不按季区分时为 None
    :param episode_group: 剧集组标识；未指定时使用来源默认剧集编排
    :param music_type: 音乐实体类型：recording 单曲、album 专辑
    """

    media_source: MediaSource
    media_id: str
    type: Optional[str] = None
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None

    def to_payload(self) -> dict[str, JsonData]:
        """
        返回新增查重可消费的身份字典。
        """
        return {
            "media_source": str(self.media_source),
            "media_id": self.media_id,
            "type": self.type,
            "season": self.season,
            "episode_group": self.episode_group,
            "music_type": self.music_type,
        }


@dataclass(frozen=True, slots=True)
class SubscriptionPatch:
    """
    一次订阅新增、修改或完成允许写入的字段集合。

    :param values: 允许写入活动订阅的字段和值，构造时检查白名单并冻结嵌套 JSON
    """

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """
        拒绝表外字段并冻结调用方传入的嵌套 JSON。
        """
        unknown = sorted(set(self.values) - _SUBSCRIPTION_FIELDS)
        if unknown:
            raise ValueError(f"订阅写入包含未知字段: {', '.join(unknown)}")
        object.__setattr__(
            self, "values", _FrozenJsonDict({key: _freeze_json(value) for key, value in self.values.items()})
        )

    def to_payload(self) -> dict[str, JsonData]:
        """
        返回 数据库适配器 可修改的独立字典。
        """
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SubscriptionHistoryPatch:
    """
    完成订阅时允许写入历史表的字段白名单快照。

    :param values: 允许写入订阅历史的字段和值，构造时检查白名单并冻结嵌套 JSON
    """

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """
        拒绝历史表外字段并冻结嵌套 JSON。
        """
        unknown = sorted(set(self.values) - _SUBSCRIPTION_HISTORY_FIELDS)
        if unknown:
            raise ValueError(f"订阅历史写入包含未知字段: {', '.join(unknown)}")
        object.__setattr__(
            self,
            "values",
            _FrozenJsonDict({key: _freeze_json(value) for key, value in self.values.items()}),
        )

    @classmethod
    def from_subscription(
        cls,
        payload: Mapping[str, JsonData],
    ) -> SubscriptionHistoryPatch:
        """
        从完整订阅快照只投影历史表拥有的列。
        """
        return cls({key: value for key, value in payload.items() if key in _SUBSCRIPTION_HISTORY_FIELDS})

    def to_payload(self) -> dict[str, JsonData]:
        """
        返回 数据库适配器 可修改的历史字段字典。
        """
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SubscriptionWriteResult:
    """
    订阅新增暂存结果。

    :param subscribe_id: 订阅记录的数据库主键
    :param message: 新增或查重结果的可读说明
    :param created: 是否实际新建订阅，已有记录时为 False
    """

    subscribe_id: int
    message: str
    created: bool


@dataclass(frozen=True, slots=True)
class SubscribeDeletionCandidate:
    """
    删除前读取的订阅归属与事件快照。

    :param subscribe_id: 订阅记录的数据库主键
    :param username: 订阅所属的本地用户名；None 表示未绑定用户
    :param event_payload: 用于发布订阅业务事件的字段快照
    """

    subscribe_id: int
    username: Optional[str]
    event_payload: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """
        冻结删除事件快照，隔离 ORM JSON 列。
        """
        object.__setattr__(
            self,
            "event_payload",
            _FrozenJsonDict({key: _freeze_json(value) for key, value in self.event_payload.items()}),
        )


AfterCommitEffect = Callable[[int], bool | None]
AsyncAfterCommitEffect = Callable[[int], Awaitable[bool | None]]


def subscription_added_event_key(
    subscribe_id: int,
    payload: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """
    由订阅 ID、媒体身份与本次创建事实构造可重试且不复用的幂等键。
    """
    resolved_occurrence_id = occurrence_id or uuid4().hex
    return (
        f"subscribe.added:{subscribe_id}:"
        f"{payload.get('media_source') or 'unknown'}:"
        f"{payload.get('media_id') or 'unknown'}:{resolved_occurrence_id}:v1"
    )


def subscription_added_report_key(
    subscribe_id: int,
    payload: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """
    返回与新增事件身份一致但可独立重试的统计幂等键。
    """
    event_key = subscription_added_event_key(
        subscribe_id,
        payload,
        occurrence_id=occurrence_id,
    )
    return f"{event_key}:report"


def subscription_added_notification_key(
    subscribe_id: int,
    payload: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """
    构造订阅新增通知的稳定幂等键。
    """
    event_key = subscription_added_event_key(
        subscribe_id,
        payload,
        occurrence_id=occurrence_id,
    )
    return f"{event_key}:notification"


class SubscriptionQueryPort(Protocol):
    """
    Chain、Workflow、Agent 和 Application 共用的订阅查询端口。
    """

    def exists(self, identity: SubscriptionIdentity) -> bool:
        """
        同步判断完整媒体身份是否已有订阅。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :return: 存在完整身份匹配的活动订阅时返回 True
        """
        ...

    def get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """
        同步按主键读取订阅快照。

        :param subscribe_id: 活动订阅的数据库主键
        :return: 目标订阅的会话外快照；不存在时返回 None
        """
        ...

    def get_by(self, identity: SubscriptionIdentity) -> Optional[SubscriptionSnapshot]:
        """
        同步按来源身份读取订阅快照。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :return: 匹配媒体身份的订阅快照；不存在时返回 None
        """
        ...

    def list(self, state: Optional[str] = None) -> builtins.list[SubscriptionSnapshot]:
        """
        同步按可选状态读取订阅快照。

        :param state: 可选订阅状态筛选；None 表示不限制
        :return: 满足状态筛选的订阅快照列表，无结果时为空列表
        """
        ...

    async def async_get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """
        异步按主键读取订阅快照。

        :param subscribe_id: 活动订阅的数据库主键
        :return: 目标订阅的会话外快照；不存在时返回 None
        """
        ...

    async def async_list(
        self,
        state: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """
        异步按可选状态和窗口读取订阅快照。

        :param state: 可选订阅状态筛选；None 表示不限制
        :param page: 页码，从 1 开始；可选窗口仅在 page 和 count 均提供时生效
        :param count: 每页最多返回的记录数
        :return: 满足状态与分页条件的订阅快照列表
        """
        ...

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """
        异步按用户、状态、类型和窗口读取订阅快照。

        :param username: 用于筛选订阅归属的本地用户名
        :param state: 可选订阅状态筛选；None 表示不限制
        :param mtype: 媒体类型筛选；None 表示不限制
        :param page: 页码，从 1 开始；可选窗口仅在 page 和 count 均提供时生效
        :param count: 每页最多返回的记录数
        :return: 属于指定用户且满足其他筛选条件的订阅快照列表
        """
        ...

    async def async_count(
        self,
        state: Optional[str] = None,
        username: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> int:
        """
        按与公开列表相同的筛选范围返回订阅总数。

        :param state: 可选订阅状态筛选；None 表示不限制
        :param username: 订阅归属用户筛选；None 表示不限用户
        :param mtype: 媒体类型筛选；None 表示不限制
        :return: 满足相同筛选条件的活动订阅总数
        """
        ...

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """
        异步按规范媒体身份读取订阅快照。

        :param media_source: 标准媒体来源，与 media_id 配对使用
        :param media_id: 该媒体来源中的原生标识
        :param music_type: 音乐实体类型筛选；None 表示不附加实体类型限制
        :return: 匹配来源身份及音乐实体类型的订阅快照列表
        """
        ...

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """
        异步按标题和季读取订阅快照。

        :param title: 用于匹配的媒体标题
        :param season: 要匹配的季号；None 表示不限定季号
        :return: 匹配标题和可选季号的订阅快照列表
        """
        ...

    def history_exists(self, identity: SubscriptionIdentity) -> bool:
        """
        同步判断完整媒体身份是否已有订阅历史。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :return: 存在完整身份匹配的订阅历史时返回 True
        """
        ...


class SubscriptionHistoryQueryPort(Protocol):
    """
    订阅历史公开查询端口。
    """

    async def async_get(self, history_id: int) -> Optional[SubscriptionHistorySnapshot]:
        """
        异步按主键读取订阅历史快照。

        :param history_id: 订阅历史记录的数据库主键
        :return: 匹配的订阅历史快照；不存在时返回 None
        """
        ...

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """
        异步按类型分页读取订阅历史快照。

        :param mtype: 用于筛选订阅历史的媒体类型
        :param page: 页码，从 1 开始，默认读取第一页
        :param count: 每页最多返回的记录数
        :return: 指定媒体类型的订阅历史快照列表
        """
        ...

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """
        异步按类型和用户分页读取订阅历史快照。

        :param mtype: 用于筛选订阅历史的媒体类型
        :param username: 用于筛选订阅归属的本地用户名
        :param page: 页码，从 1 开始，默认读取第一页
        :param count: 每页最多返回的记录数
        :return: 指定媒体类型和用户的订阅历史快照列表
        """
        ...

    async def async_count_by_type(self, mtype: str) -> int:
        """
        异步统计指定媒体类型的订阅历史。

        :param mtype: 用于筛选订阅历史的媒体类型
        :return: 指定媒体类型的订阅历史数量
        """
        ...

    async def async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """
        异步统计指定媒体类型和用户的订阅历史。

        :param mtype: 用于筛选订阅历史的媒体类型
        :param username: 用于筛选订阅归属的本地用户名
        :return: 指定媒体类型和用户的订阅历史数量
        """
        ...


class SubscriptionWritePort(Protocol):
    """
    独立短事务订阅新增端口。
    """

    def add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
        notification: Optional[Mapping[str, JsonData]] = None,
        occurrence_id: Optional[str] = None,
    ) -> tuple[int, str]:
        """
        在独立同步事务中新增订阅；occurrence_id 标识本次创建事实。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :param payload: 经白名单约束的订阅写入字段
        :param username: 查重范围：None 表示跨用户查重，空字符串跳过查重，其他值只查该用户；新记录归属取自 payload
        :param after_commit: 新建提交后或查重命中已有订阅时执行的回调；None 表示无附加回调
        :param notification: 提交后通知使用的字段快照；None 表示不附加通知意图
        :param occurrence_id: 本次创建事实标识，参与副作用幂等键；None 时由实现生成
        :return: 订阅主键与结果说明组成的元组；已有订阅返回原主键，未能新增时主键为 0
        """
        ...

    async def async_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
        after_commit: Optional[AsyncAfterCommitEffect] = None,
        notification: Optional[Mapping[str, JsonData]] = None,
        occurrence_id: Optional[str] = None,
    ) -> tuple[int, str]:
        """
        在独立异步事务中新增订阅；occurrence_id 标识本次创建事实。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :param payload: 经白名单约束的订阅写入字段
        :param username: 查重范围：None 表示跨用户查重，空字符串跳过查重，其他值只查该用户；新记录归属取自 payload
        :param after_commit: 新建提交后或查重命中已有订阅时执行的回调；None 表示无附加回调
        :param notification: 提交后通知使用的字段快照；None 表示不附加通知意图
        :param occurrence_id: 本次创建事实标识，参与副作用幂等键；None 时由实现生成
        :return: 订阅主键与结果说明组成的元组；已有订阅返回原主键，未能新增时主键为 0
        """
        ...


class SubscriptionStagingPort(Protocol):
    """
    复用调用方 Session 且不自行提交的订阅写端口。
    """

    def stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """
        同步暂存新增订阅。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :param payload: 经白名单约束的订阅写入字段
        :param username: 查重范围：None 表示跨用户查重，空字符串跳过查重，其他值只查该用户；新记录归属取自 payload
        :return: 暂存的订阅主键、说明和是否新建标记，不表示事务已经提交
        """
        ...

    async def async_stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """
        异步暂存新增订阅。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :param payload: 经白名单约束的订阅写入字段
        :param username: 查重范围：None 表示跨用户查重，空字符串跳过查重，其他值只查该用户；新记录归属取自 payload
        :return: 暂存的订阅主键、说明和是否新建标记，不表示事务已经提交
        """
        ...

    async def get_candidate(self, subscribe_id: int) -> Optional[SubscribeDeletionCandidate]:
        """
        异步读取删除候选快照。

        :param subscribe_id: 活动订阅的数据库主键
        :return: 删除前的归属及事件快照；订阅不存在时返回 None
        """
        ...

    def get_candidate_sync(self, subscribe_id: int) -> Optional[SubscribeDeletionCandidate]:
        """
        同步读取删除候选快照。

        :param subscribe_id: 活动订阅的数据库主键
        :return: 删除前的归属及事件快照；订阅不存在时返回 None
        """
        ...

    async def list_candidates_by_identity(
        self,
        identity: SubscriptionIdentity,
    ) -> builtins.list[SubscribeDeletionCandidate]:
        """
        异步按媒体身份读取删除候选快照。

        :param identity: 含来源、原生标识及必要季集维度的订阅身份
        :return: 匹配媒体身份的删除候选快照列表
        """
        ...

    async def list_search_ids(
        self,
        username: Optional[str],
        state: str,
        mtype: Optional[str] = None,
    ) -> builtins.list[int]:
        """
        异步读取用户或管理员指定媒体类型范围内可搜索的订阅主键。

        :param username: 订阅归属用户筛选；None 表示不限用户
        :param state: 可搜索订阅的状态筛选
        :param mtype: 媒体类型筛选；None 表示不限制
        :return: 指定用户、状态和媒体类型范围内的可搜索订阅主键
        """
        ...

    async def stage_delete(self, subscribe_id: int) -> None:
        """
        异步暂存删除订阅。

        :param subscribe_id: 活动订阅的数据库主键
        """
        ...

    def stage_delete_sync(self, subscribe_id: int) -> None:
        """
        同步暂存删除订阅。

        :param subscribe_id: 活动订阅的数据库主键
        """
        ...

    def stage_history(self, payload: SubscriptionHistoryPatch) -> None:
        """
        同步暂存订阅历史。

        :param payload: 经历史字段白名单约束的订阅完成快照
        """
        ...


class SubscriptionHistoryStagingPort(SubscriptionHistoryQueryPort, Protocol):
    """
    复用请求 Session 的订阅历史删除端口。
    """

    async def stage_delete(self, history_id: int) -> None:
        """
        异步暂存删除订阅历史。

        :param history_id: 订阅历史记录的数据库主键
        """
        ...


class SubscriptionMutationPort(Protocol):
    """
    订阅修改服务在调用方 Session 内暂存更新的最小端口。
    """

    def stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """
        同步暂存更新并返回事务内快照。

        :param subscribe_id: 活动订阅的数据库主键
        :param patch: 经白名单约束的订阅更新字段
        :return: 更新后的事务内快照；订阅不存在时返回 None
        """
        ...

    async def async_stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """
        在调用方事务中暂存更新并返回订阅快照。

        :param subscribe_id: 活动订阅的数据库主键
        :param patch: 经白名单约束的订阅更新字段
        :return: 更新后的事务内快照；订阅不存在时返回 None
        """
        ...


class SubscriptionReferenceStagingPort(SubscriptionMutationPort, Protocol):
    """
    跨表引用重写所需的订阅锁定与暂存端口。
    """

    def list_for_reference_rewrite(self) -> builtins.list[SubscriptionSnapshot]:
        """
        同步锁定并返回全部订阅快照。

        :return: 已锁定的全部订阅快照，锁随调用方事务结束释放
        """
        ...

    async def async_list_for_reference_rewrite(
        self,
    ) -> builtins.list[SubscriptionSnapshot]:
        """
        异步锁定并返回全部订阅快照。

        :return: 已锁定的全部订阅快照，锁随调用方事务结束释放
        """
        ...


class SubscriptionRepository(
    SubscriptionQueryPort,
    SubscriptionWritePort,
    Protocol,
):
    """
    组合宿主独立短事务查询与新增能力。
    """


class SessionSubscriptionPort(
    SubscriptionQueryPort,
    SubscriptionMutationPort,
    SubscriptionStagingPort,
    Protocol,
):
    """
    复用请求 Session 的订阅查询、修改与暂存组合端口。
    """


def build_subscribe_meta(subscribe: SubscriptionSnapshot) -> MetaBase:
    """
    按订阅快照构造主程序链路共用的媒体元数据。
    """
    if subscribe.type == MediaType.MUSIC.value:
        is_album = subscribe.music_type == MUSIC_ENTITY_ALBUM
        music_year: Optional[int] = None
        if subscribe.year:
            try:
                music_year = int(subscribe.year)
            except ValueError:
                pass
        return MetaMusic(
            title=subscribe.name,
            album=subscribe.name if is_album else None,
            year=music_year,
            total_tracks=subscribe.total_tracks if is_album else None,
            media_source=subscribe.media_source,
            media_id=subscribe.media_id,
        )
    meta = MetaInfo(subscribe.name)
    meta.year = subscribe.year
    meta.begin_season = subscribe.season
    if subscribe.type:
        meta.type = MediaType(subscribe.type)
    meta.media_source = subscribe.media_source
    meta.media_id = subscribe.media_id
    return meta


def subscribe_media_key(subscribe: SubscriptionSnapshot) -> str | int | None:
    """
    返回订阅缺失集映射使用的稳定媒体键。
    """
    media_source, media_id = resolve_media_identity(media=subscribe)
    return cast(str | int | None, build_media_key(media_source, media_id) or media_id)


def subscribe_media_keys(subscribe: SubscriptionSnapshot) -> builtins.list[str | int]:
    """
    返回缺失集缓存可识别的规范媒体键与旧纯 ID 键。
    """
    media_source, media_id = resolve_media_identity(media=subscribe)
    candidates = [build_media_key(media_source, media_id), media_id]
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, (str, int)) and candidate != ""
    ]
