"""订阅创建与提交后阶段使用的不可变上下文。"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from uuid import uuid4

from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.schemas.types import (
    MediaSource,
    MediaType,
    NotificationChannel,
)


@dataclass(frozen=True, slots=True)
class _SubscribePostCommitContext:
    """订阅提交后副作用所需的不可变业务快照。"""

    title: str
    year: str
    metainfo: MetaBase
    mediainfo: MediaInfo
    media_source: Optional[MediaSource]
    media_id: Optional[str]
    season: Optional[int]
    channel: Optional[NotificationChannel]
    source: Optional[str]
    userid: Optional[str]
    username: Optional[str]
    message: bool
    notification: Optional[dict[str, Any]] = None
    occurrence_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(slots=True)
class _SubscribeCreateContext:
    """订阅新增各阶段共享的显式状态，避免同步与异步入口各自维护散落变量。"""

    title: str
    year: str
    mtype: Optional[MediaType]
    episode_group: Optional[str]
    season: Optional[int]
    channel: Optional[NotificationChannel]
    source: Optional[str]
    userid: Optional[str]
    username: Optional[str]
    message: bool
    exist_ok: bool
    options: Dict[str, Any]
    explicit_identity: bool
    media_source: Optional[MediaSource]
    media_id: Optional[str]
    requested_music_type: Optional[str]
    metainfo: MetaBase
    mediainfo: Optional[MediaInfo] = None
