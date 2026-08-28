"""订阅识别调用所需的媒体身份投影。"""

from typing import Any

from app.application.subscription.contract import SubscriptionSnapshot
from app.domain.context import MediaInfo
from app.schemas.media import resolve_media_identity


def media_recognize_kwargs(mediainfo: MediaInfo) -> dict[str, Any]:
    """从统一媒体信息构造规范识别身份参数。"""
    media_source, media_id = resolve_media_identity(media=mediainfo)
    return {
        "media_source": media_source,
        "media_id": media_id,
    }


def subscribe_recognize_kwargs(subscribe: SubscriptionSnapshot) -> dict[str, Any]:
    """从订阅快照构造规范识别身份参数。"""
    media_source, media_id = resolve_media_identity(media=subscribe)
    return {
        "media_source": media_source,
        "media_id": media_id,
    }
