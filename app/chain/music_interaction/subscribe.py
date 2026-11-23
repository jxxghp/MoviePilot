"""
消息渠道媒体交互的订阅创建与重试提示。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from app.application.security.authorization import get_channel_authorization
from app.application.subscription.write import SubscriptionAuthorizationError
from app.chain.download import DownloadChain
from app.chain.music_interaction.music import (
    media_exists_retry_prompt,
    selected_music_type,
)
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.message import Message
from app.schemas.types import NotificationChannel

if TYPE_CHECKING:
    from app.application.messaging.media import PendingMediaInteraction
    from app.application.security.user import ChainUserRepository


class MediaInteractionSubscribeHost(Protocol):
    """
    声明订阅创建复用的用户名称查询与消息回复能力。

    :ivar user_repository: 按渠道绑定身份查询 MoviePilot 用户名的用户仓库
    """

    user_repository: ChainUserRepository

    def post_message(self, message: Message) -> None:
        """
        将媒体库重复提示发送回当前消息渠道。

        :param message: 带原始渠道、接收人和重复订阅提示的通知消息
        """
        ...


def subscribe_media(
    host: MediaInteractionSubscribeHost,
    request: PendingMediaInteraction,
    mediainfo: MediaInfo | MusicInfo,
    channel: NotificationChannel,
    source: str,
    userid: int | str,
    username: str,
    owner_username: str | None = None,
) -> None:
    """
    为已选影视或音乐目标创建普通订阅或音质洗版订阅。

    :param host: 提供用户绑定查询与渠道消息回复能力的宿主对象
    :param request: 当前媒体交互请求上下文
    :param mediainfo: 用户选定的影视或音乐目标
    :param channel: 发起交互的通知渠道
    :param source: 渠道实例标识
    :param userid: 渠道平台发送者标识
    :param username: 渠道展示昵称，未解析到业务归属时兜底使用
    :param owner_username: 统一渠道授权已确定的业务归属用户名；为空时回退按渠道绑定查询
    """
    service = get_channel_authorization()
    owner = None
    if service is not None and channel not in {NotificationChannel.Web, NotificationChannel.WebAgent}:
        try:
            owner = service.resolve_operation(channel.value, source, str(userid), "subscription.add")
        except PermissionError as error:
            host.post_message(Message(
                channel=channel, source=source, userid=userid, title=str(error), save_history=False,
                original_chat_id=request.original_chat_id,
            ))
            return
    best_version = request.action in {"ReSubscribe", "MusicReSubscribe"}
    if not best_version:
        exist_flag, _ = DownloadChain().get_no_exists_info(
            meta=request.meta,
            mediainfo=mediainfo,
        )
        if exist_flag:
            media_label, retry_hint = media_exists_retry_prompt(
                mediainfo=mediainfo,
                meta=request.meta,
                keyword=request.keyword,
                subscription=True,
            )
            host.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"【{media_label} 媒体库中已存在，如需洗版请发送：{retry_hint}】",
                    save_history=False, original_chat_id=request.original_chat_id,
                )
            )
            return

    mp_name = owner.name if owner else (owner_username or (
        host.user_repository.find_name_by_bindings(
            {f"{channel.name.lower()}_userid": userid}
        )
    ))
    submit_media_subscription(
        host, channel, source, userid, request.original_chat_id,
        title=mediainfo.title or "",
        year=str(mediainfo.year or ""),
        mtype=mediainfo.type,
        media_source=mediainfo.media_source,
        media_id=mediainfo.media_id,
        season=request.meta.begin_season if request.meta else None,
        username=mp_name or username,
        owner_identity=owner.notification_identity if owner else None,
        owner_scope=bool(owner),
        best_version=best_version,
        music_type=selected_music_type(mediainfo),
    )


def submit_media_subscription(
    host: MediaInteractionSubscribeHost,
    channel: NotificationChannel,
    source: str,
    userid: int | str,
    original_chat_id: str | None,
    *,
    owner_identity: str | None,
    **options: Any,
) -> None:
    """
    在订阅写入前复核发言人的当前权限，将拒绝反馈到本次会话。

    :param host: 发送订阅结果的交互宿主
    :param channel: 原交互渠道
    :param source: 原连接名称
    :param userid: 发起请求的平台个人身份
    :param original_chat_id: 原会话回复地址
    :param owner_identity: 服务器核对过的订阅归属
    :param options: 本次已选订阅的创建参数
    """
    def authorize() -> None:
        """
        核对媒体准备后的账号和订阅权限。
        """
        service = get_channel_authorization()
        if service is None:
            raise PermissionError("当前渠道授权不可用，请重新发起订阅")
        actor = service.resolve_operation(channel.value, source, str(userid), "subscription.add")
        service.verify_operation_owner(actor, owner_identity)

    try:
        SubscribeChain().add(
            channel=channel, source=source, userid=str(userid),
            _authorize=authorize if owner_identity is not None else None,
            owner_identity=owner_identity, **options,
        )
    except SubscriptionAuthorizationError as error:
        host.post_message(Message(
            channel=channel, source=source, userid=userid, title=str(error),
            save_history=False, original_chat_id=original_chat_id,
        ))
