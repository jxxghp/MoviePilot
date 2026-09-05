"""订阅持久化快照、写入数据与端口合同。"""

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

_SUBSCRIPTION_FIELDS = frozenset(
    {
        "name",
        "year",
        "type",
        "keyword",
        "media_source",
        "media_id",
        "music_type",
        "total_tracks",
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
    "state",
    "last_update",
    "downloader",
    "manual_total_episode",
}) | _CLASSIFICATION_HISTORY_FIELDS


class _FrozenJsonDict(dict[str, JsonData]):
    """保留 JSON 字典读取语义并拒绝原地修改。"""

    def _reject(self, *args: object, **kwargs: object) -> NoReturn:
        """拒绝修改订阅快照内的 JSON。"""
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
    """保留 JSON 数组读取语义并拒绝原地修改。"""

    def _reject(self, *args: object, **kwargs: object) -> NoReturn:
        """拒绝修改订阅快照内的 JSON。"""
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
    """递归复制并冻结 JSON 容器。"""
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


def _copy_json(value: JsonData) -> JsonData:
    """把冻结 JSON 递归复制为普通容器。"""
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """脱离 Session 的完整订阅快照。"""

    id: int
    name: str
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
    lack_episode: Optional[int] = None
    note: Optional[builtins.list[int]] = None
    state: str = "N"
    last_update: Optional[str] = None
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

    def __post_init__(self) -> None:
        """冻结订阅中的全部 JSON 列。"""
        for name in ("note", "sites", "episode_priority", "filter_groups"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))

    def to_dict(self) -> dict[str, JsonData]:
        """返回事件和响应可安全修改的独立字典。"""
        return {field.name: _copy_json(cast(JsonData, getattr(self, field.name))) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SubscriptionHistorySnapshot:
    """脱离 Session 的完整订阅历史快照。"""

    id: int
    name: str
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

    def __post_init__(self) -> None:
        """冻结历史中的全部 JSON 列。"""
        for name in ("sites", "episode_priority", "filter_groups"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))

    def to_dict(self) -> dict[str, JsonData]:
        """返回事件和响应可安全修改的独立字典。"""
        return {field.name: _copy_json(cast(JsonData, getattr(self, field.name))) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SubscriptionIdentity:
    """订阅去重与来源定位使用的明确媒体身份。"""

    media_source: MediaSource
    media_id: str
    type: Optional[str] = None
    season: Optional[int] = None
    episode_group: Optional[str] = None
    music_type: Optional[str] = None

    def to_payload(self) -> dict[str, JsonData]:
        """返回新增查重可消费的身份字典。"""
        return {
            "media_source": str(self.media_source),
            "media_id": self.media_id,
            "season": self.season,
            "episode_group": self.episode_group,
            "music_type": self.music_type,
        }


@dataclass(frozen=True, slots=True)
class SubscriptionPatch:
    """一次订阅新增、修改或完成允许写入的字段集合。"""

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """拒绝表外字段并冻结调用方传入的嵌套 JSON。"""
        unknown = sorted(set(self.values) - _SUBSCRIPTION_FIELDS)
        if unknown:
            raise ValueError(f"订阅写入包含未知字段: {', '.join(unknown)}")
        object.__setattr__(
            self, "values", _FrozenJsonDict({key: _freeze_json(value) for key, value in self.values.items()})
        )

    def to_payload(self) -> dict[str, JsonData]:
        """返回 DB adapter 可修改的独立字典。"""
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SubscriptionHistoryPatch:
    """完成订阅时允许写入历史表的字段白名单快照。"""

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """拒绝历史表外字段并冻结嵌套 JSON。"""
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
        """从完整订阅快照只投影历史表拥有的列。"""
        return cls({key: value for key, value in payload.items() if key in _SUBSCRIPTION_HISTORY_FIELDS})

    def to_payload(self) -> dict[str, JsonData]:
        """返回 DB adapter 可修改的历史字段字典。"""
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SubscriptionWriteResult:
    """订阅新增暂存结果。"""

    subscribe_id: int
    message: str
    created: bool


@dataclass(frozen=True, slots=True)
class SubscribeDeletionCandidate:
    """删除前读取的订阅归属与事件快照。"""

    subscribe_id: int
    username: Optional[str]
    event_payload: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """冻结删除事件快照，隔离 ORM JSON 列。"""
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
    """由订阅 ID、媒体身份与本次创建事实构造可重试且不复用的幂等键。"""
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
    """返回与新增事件身份一致但可独立重试的统计幂等键。"""
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
    """构造订阅新增通知的稳定幂等键。"""
    event_key = subscription_added_event_key(
        subscribe_id,
        payload,
        occurrence_id=occurrence_id,
    )
    return f"{event_key}:notification"


class SubscriptionQueryPort(Protocol):
    """Chain、Workflow、Agent 和 Application 共用的订阅查询端口。"""

    def exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断完整媒体身份是否已有订阅。"""
        ...

    def get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """同步按主键读取订阅快照。"""
        ...

    def get_by(self, identity: SubscriptionIdentity) -> Optional[SubscriptionSnapshot]:
        """同步按来源身份读取订阅快照。"""
        ...

    def list(self, state: Optional[str] = None) -> builtins.list[SubscriptionSnapshot]:
        """同步按可选状态读取订阅快照。"""
        ...

    async def async_get(self, subscribe_id: int) -> Optional[SubscriptionSnapshot]:
        """异步按主键读取订阅快照。"""
        ...

    async def async_list(
        self,
        state: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按可选状态和窗口读取订阅快照。"""
        ...

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按用户、状态、类型和窗口读取订阅快照。"""
        ...

    async def async_count(
        self,
        state: Optional[str] = None,
        username: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> int:
        """按与公开列表相同的筛选范围返回订阅总数。"""
        ...

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按规范媒体身份读取订阅快照。"""
        ...

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步按标题和季读取订阅快照。"""
        ...

    def history_exists(self, identity: SubscriptionIdentity) -> bool:
        """同步判断完整媒体身份是否已有订阅历史。"""
        ...


class SubscriptionHistoryQueryPort(Protocol):
    """订阅历史公开查询端口。"""

    async def async_get(self, history_id: int) -> Optional[SubscriptionHistorySnapshot]:
        """异步按主键读取订阅历史快照。"""
        ...

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """异步按类型分页读取订阅历史快照。"""
        ...

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> builtins.list[SubscriptionHistorySnapshot]:
        """异步按类型和用户分页读取订阅历史快照。"""
        ...

    async def async_count_by_type(self, mtype: str) -> int:
        """异步统计指定媒体类型的订阅历史。"""
        ...

    async def async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """异步统计指定媒体类型和用户的订阅历史。"""
        ...


class SubscriptionWritePort(Protocol):
    """独立短事务订阅新增端口。"""

    def add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
        notification: Optional[Mapping[str, JsonData]] = None,
        occurrence_id: Optional[str] = None,
    ) -> tuple[int, str]:
        """在独立同步事务中新增订阅；occurrence_id 标识本次创建事实。"""
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
        """在独立异步事务中新增订阅；occurrence_id 标识本次创建事实。"""
        ...

class SubscriptionStagingPort(Protocol):
    """复用调用方 Session 且不自行提交的订阅写端口。"""

    def stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """同步暂存新增订阅。"""
        ...

    async def async_stage_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
    ) -> SubscriptionWriteResult:
        """异步暂存新增订阅。"""
        ...

    async def get_candidate(self, subscribe_id: int) -> Optional[SubscribeDeletionCandidate]:
        """异步读取删除候选快照。"""
        ...

    def get_candidate_sync(self, subscribe_id: int) -> Optional[SubscribeDeletionCandidate]:
        """同步读取删除候选快照。"""
        ...

    async def list_candidates_by_identity(
        self,
        identity: SubscriptionIdentity,
    ) -> builtins.list[SubscribeDeletionCandidate]:
        """异步按媒体身份读取删除候选快照。"""
        ...

    async def list_search_ids(
        self,
        username: Optional[str],
        state: str,
    ) -> builtins.list[int]:
        """异步读取用户或管理员全局范围内可搜索的订阅主键。"""
        ...

    async def stage_delete(self, subscribe_id: int) -> None:
        """异步暂存删除订阅。"""
        ...

    def stage_delete_sync(self, subscribe_id: int) -> None:
        """同步暂存删除订阅。"""
        ...

    def stage_history(self, payload: SubscriptionHistoryPatch) -> None:
        """同步暂存订阅历史。"""
        ...


class SubscriptionHistoryStagingPort(SubscriptionHistoryQueryPort, Protocol):
    """复用请求 Session 的订阅历史删除端口。"""

    async def stage_delete(self, history_id: int) -> None:
        """异步暂存删除订阅历史。"""
        ...


class SubscriptionMutationPort(Protocol):
    """订阅修改服务在调用方 Session 内暂存更新的最小端口。"""

    def stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """同步暂存更新并返回事务内快照。"""
        ...

    async def async_stage_update(
        self,
        subscribe_id: int,
        patch: SubscriptionPatch,
    ) -> Optional[SubscriptionSnapshot]:
        """在调用方事务中暂存更新并返回订阅快照。"""
        ...


class SubscriptionReferenceStagingPort(SubscriptionMutationPort, Protocol):
    """跨表引用重写所需的订阅锁定与暂存端口。"""

    def list_for_reference_rewrite(self) -> builtins.list[SubscriptionSnapshot]:
        """同步锁定并返回全部订阅快照。"""
        ...

    async def async_list_for_reference_rewrite(
        self,
    ) -> builtins.list[SubscriptionSnapshot]:
        """异步锁定并返回全部订阅快照。"""
        ...


class SubscriptionRepository(
    SubscriptionQueryPort,
    SubscriptionWritePort,
    Protocol,
):
    """组合宿主 standalone 查询与新增能力。"""


class SessionSubscriptionPort(
    SubscriptionQueryPort,
    SubscriptionMutationPort,
    SubscriptionStagingPort,
    Protocol,
):
    """复用请求 Session 的订阅查询、修改与暂存组合端口。"""


def build_subscribe_meta(subscribe: SubscriptionSnapshot) -> MetaBase:
    """按订阅快照构造主程序链路共用的媒体元数据。"""
    if subscribe.type == MediaType.MUSIC.value:
        is_album = subscribe.music_type == MUSIC_ENTITY_ALBUM
        return MetaMusic(
            title=subscribe.name,
            album=subscribe.name if is_album else None,
            year=subscribe.year,
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
    """返回订阅缺失集映射使用的稳定媒体键。"""
    media_source, media_id = resolve_media_identity(media=subscribe)
    return cast(str | int | None, build_media_key(media_source, media_id) or media_id)


def subscribe_media_keys(subscribe: SubscriptionSnapshot) -> builtins.list[str | int]:
    """返回缺失集缓存可识别的规范媒体键与旧纯 ID 键。"""
    media_source, media_id = resolve_media_identity(media=subscribe)
    candidates = [build_media_key(media_source, media_id), media_id]
    return [candidate for candidate in candidates if candidate not in (None, "")]
