"""中心服务订阅载荷的共享投影。"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from app.schemas.media import resolve_media_identity


def build_subscribe_payload(
    item: object,
    fields: Collection[str],
) -> dict[str, Any] | None:
    """过滤本地订阅字段并规范化中心服务使用的媒体身份。"""
    if not isinstance(item, dict):
        return None
    media_source, media_id = resolve_media_identity(media=item)
    if not media_source or not media_id:
        return None
    payload = {key: value for key, value in item.items() if key in fields}
    payload["media_source"] = str(media_source)
    payload["media_id"] = media_id
    return payload
