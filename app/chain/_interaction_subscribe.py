"""消息渠道媒体交互的订阅创建与重试提示。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.chain._music_interaction import (
    media_exists_retry_prompt,
    selected_music_type,
)
from app.chain.download import DownloadChain
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.message import Message
from app.schemas.types import NotificationChannel

if TYPE_CHECKING:
    from app.application.messaging.media import PendingMediaInteraction
    from app.application.security.user import ChainUserRepository


class MediaInteractionSubscribeHost(Protocol):
    """声明订阅创建复用的用户名称查询与消息回复能力。"""

    user_repository: ChainUserRepository

    def post_message(self, message: Message) -> None:
        """将媒体库重复提示发送回当前消息渠道。"""
        ...


def subscribe_media(
    host: MediaInteractionSubscribeHost,
    request: PendingMediaInteraction,
    mediainfo: MediaInfo | MusicInfo,
    channel: NotificationChannel,
    source: str,
    userid: int | str,
    username: str,
) -> None:
    """为已选影视或音乐目标创建普通订阅或音质洗版订阅。"""
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
                    save_history=False,
                )
            )
            return

    mp_name = (
        host.user_repository.find_name_by_bindings(
            {f"{channel.name.lower()}_userid": userid}
        )
        if channel
        else None
    )
    SubscribeChain().add(
        title=mediainfo.title or "",
        year=str(mediainfo.year or ""),
        mtype=mediainfo.type,
        media_source=mediainfo.media_source,
        media_id=mediainfo.media_id,
        season=request.meta.begin_season if request.meta else None,
        channel=channel,
        source=source,
        userid=str(userid),
        username=mp_name or username,
        best_version=best_version,
        music_type=selected_music_type(mediainfo),
    )
