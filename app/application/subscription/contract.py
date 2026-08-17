"""订阅编排共享的媒体元数据与身份契约。"""

from typing import Optional, Protocol, Union

from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType


class SubscribeSnapshot(Protocol):
    """构造订阅媒体契约所需的最小只读字段集合。"""

    name: str
    type: str
    year: Optional[str]
    season: Optional[int]
    media_source: object
    media_id: object
    music_type: Optional[str]
    total_tracks: Optional[int]


def build_subscribe_meta(subscribe: SubscribeSnapshot) -> MetaBase:
    """按订阅快照构造主程序链路共用的媒体元数据。"""
    if subscribe.type == MediaType.MUSIC.value:
        is_album = getattr(subscribe, "music_type", None) == MUSIC_ENTITY_ALBUM
        return MetaMusic(
            title=subscribe.name,
            album=subscribe.name if is_album else None,
            year=subscribe.year,
            total_tracks=(
                getattr(subscribe, "total_tracks", None) if is_album else None
            ),
            media_source=subscribe.media_source,
            media_id=(
                str(subscribe.media_id)
                if subscribe.media_id is not None
                else None
            ),
        )
    meta = MetaInfo(subscribe.name)
    meta.year = subscribe.year
    meta.begin_season = subscribe.season
    meta.type = MediaType(subscribe.type)
    meta.media_source = subscribe.media_source
    meta.media_id = subscribe.media_id
    return meta


def subscribe_media_key(
    subscribe: SubscribeSnapshot,
) -> Union[str, int, None]:
    """返回订阅缺失集映射使用的稳定媒体键。"""
    media_source, media_id = resolve_media_identity(media=subscribe)
    return build_media_key(media_source, media_id) or media_id


def subscribe_media_keys(subscribe: SubscribeSnapshot) -> list[Union[str, int]]:
    """返回缺失集缓存可识别的规范媒体键与旧纯 ID 键。"""
    media_source, media_id = resolve_media_identity(media=subscribe)
    candidates = [
        build_media_key(media_source, media_id),
        media_id,
    ]
    return [candidate for candidate in candidates if candidate not in (None, "")]
