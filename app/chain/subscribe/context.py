"""
订阅创建与提交后阶段使用的不可变上下文。
"""

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
    """
    订阅提交后副作用所需的不可变业务快照。

    :param title: 请求使用的媒体标题，识别后可更新为规范标题
    :param year: 请求使用的发行年份文本
    :param metainfo: 订阅标题解析得到并经过入口修正的元数据
    :param mediainfo: 识别完成并用于事件与统计的媒体信息快照
    :param media_source: 规范化的媒体身份来源，None 表示尚未确定
    :param media_id: 对应来源的原生媒体标识，None 表示尚未确定
    :param season: 订阅的电视剧季号，None 表示未指定
    :param channel: 订阅结果通知渠道，None 表示由通知路由选择
    :param source: 发起订阅的消息入口来源，None 表示未提供
    :param userid: 接收订阅反馈的渠道用户标识，None 表示未指定
    :param username: 订阅归属与通知使用的用户名，None 表示未指定
    :param message: 是否生成订阅结果通知
    :param notification: 事务提交前渲染的通知消息字典，None 表示无需发送
    :param occurrence_id: 本次新增事件的唯一标识，默认生成随机值，参与事件幂等键
    """

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
    """
    订阅新增各阶段共享的显式状态，避免同步与异步入口各自维护散落变量。

    :param title: 请求使用的媒体标题，识别后可更新为规范标题
    :param year: 请求使用的发行年份文本
    :param mtype: 调用方限定的媒体类型，None 时由标题解析或识别决定
    :param episode_group: 电视剧使用的剧集组标识，None 表示未指定
    :param season: 订阅的电视剧季号，None 表示未指定
    :param channel: 订阅结果通知渠道，None 表示由通知路由选择
    :param source: 发起订阅的消息入口来源，None 表示未提供
    :param userid: 接收订阅反馈的渠道用户标识，None 表示未指定
    :param username: 订阅归属与通知使用的用户名，None 表示未指定
    :param message: 是否生成订阅结果通知
    :param exist_ok: 是否容忍订阅已存在等创建失败，启用时不发送失败通知
    :param options: 订阅过滤、保存路径、音乐实体等附加选项
    :param explicit_identity: 调用方是否显式提供了来源或媒体标识，用于拒绝不完整身份
    :param media_source: 规范化的媒体身份来源，None 表示尚未确定
    :param media_id: 对应来源的原生媒体标识，None 表示尚未确定
    :param requested_music_type: 调用方指定的音乐实体类型，None 时按识别规则选择
    :param metainfo: 订阅标题解析得到并经过入口修正的元数据
    :param mediainfo: 识别完成后的媒体信息，准备阶段尚未识别时为 None
    """

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
