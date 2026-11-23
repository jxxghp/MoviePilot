"""
订阅识别调用所需的媒体身份投影。
"""

from typing import Any

from app.application.subscription.contract import SubscriptionIdentity, SubscriptionQueryPort, SubscriptionSnapshot
from app.application.subscription.lookup import SubscriptionIdentityLookup
from app.chain.media import MediaChain
from app.chain.subscribe.context import _SubscribeCreateContext
from app.domain.context import MediaInfo
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaType


def create_subscription_identity(context: _SubscribeCreateContext) -> SubscriptionIdentity:
    """
    从最终识别结果提取创建查重身份，保持用户选择的季和剧集组。
    """
    source, media_id = resolve_media_identity(media=context.mediainfo)
    if source is None or media_id is None or context.mediainfo is None:
        raise ValueError("订阅创建缺少完整媒体身份")
    return SubscriptionIdentity(
        media_source=source, media_id=media_id,
        type=context.mediainfo.type.value if context.mediainfo.type else None,
        season=context.season, episode_group=context.mediainfo.episode_group,
    )


def find_existing_subscription(
    context: _SubscribeCreateContext, repository: SubscriptionQueryPort,
) -> SubscriptionSnapshot | None:
    """
    在持久化前同步确认同用户的跨来源订阅，音乐和显式空用户沿用原查重规则。
    """
    if context.mediainfo is None or context.mediainfo.type == MediaType.MUSIC or context.username == "":
        return None
    media = MediaChain()
    lookup = SubscriptionIdentityLookup(media.convert_media_identity, media.async_convert_media_identity)
    records = repository.list()
    owner = context.options.get("owner_identity")
    if owner:
        records = [record for record in records if record.owner_identity == owner]
    matches = lookup.find(create_subscription_identity(context), records, None if owner else context.username)
    return matches[0] if matches else None


async def async_find_existing_subscription(
    context: _SubscribeCreateContext, repository: SubscriptionQueryPort,
) -> SubscriptionSnapshot | None:
    """
    在异步和批量创建入口复用完整身份查重，不改写旧订阅的配置与进度。
    """
    if context.mediainfo is None or context.mediainfo.type == MediaType.MUSIC or context.username == "":
        return None
    media = MediaChain()
    lookup = SubscriptionIdentityLookup(media.convert_media_identity, media.async_convert_media_identity)
    records = await repository.async_list()
    owner = context.options.get("owner_identity")
    if owner:
        records = [record for record in records if record.owner_identity == owner]
    matches = await lookup.async_find(create_subscription_identity(context), records, None if owner else context.username)
    return matches[0] if matches else None


def media_recognize_kwargs(mediainfo: MediaInfo) -> dict[str, Any]:
    """
    从统一媒体信息构造规范识别身份参数。
    """
    media_source, media_id = resolve_media_identity(media=mediainfo)
    return {
        "media_source": media_source,
        "media_id": media_id,
    }


def subscribe_recognize_kwargs(subscribe: SubscriptionSnapshot) -> dict[str, Any]:
    """
    从订阅快照构造规范识别身份参数。
    """
    media_source, media_id = resolve_media_identity(media=subscribe)
    return {
        "media_source": media_source,
        "media_id": media_id,
    }
