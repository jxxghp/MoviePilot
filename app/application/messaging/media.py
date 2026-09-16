import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.application.messaging.interaction import _ExpiringUserInteractionStore
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import NotificationChannel


@dataclass
class PendingMediaInteraction:
    """
    记录一次搜索/下载/订阅交互的当前上下文。
    """

    request_id: str
    user_id: str
    channel: Optional[NotificationChannel]
    source: Optional[str]
    username: Optional[str]
    action: str
    keyword: str
    phase: str = "media"
    page: int = 0
    title: str = ""
    meta: Optional[MetaBase] = None
    current_media: Optional[MediaInfo] = None
    items: List[Any] = field(default_factory=list)
    download_dirs: List[Any] = field(default_factory=list)
    pending_download_mode: Optional[str] = None
    pending_download_context: Optional[Any] = None
    pending_no_exists: Optional[Dict[Any, Any]] = None
    pending_torrent_page: int = 0
    created_at: datetime = field(default_factory=datetime.now)


class MediaInteractionManager(
        _ExpiringUserInteractionStore[PendingMediaInteraction]
):
    """
    管理用户当前激活的媒体交互状态。

    每个用户只保留一个有效会话，避免旧按钮与新一轮搜索混用。
    """

    def create_or_replace(
            self,
            user_id: Union[str, int],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            username: Optional[str],
            action: str,
            keyword: str,
            title: str = "",
            meta: Optional[MetaBase] = None,
            items: Optional[List[Any]] = None,
    ) -> PendingMediaInteraction:
        """
        为用户创建新的交互状态，并替换旧会话。
        """
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            request = PendingMediaInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=user_key,
                channel=channel,
                source=source,
                username=username,
                action=action,
                keyword=keyword,
                title=title,
                meta=meta,
                items=list(items or []),
            )
            self._replace_locked(request)
            return request


media_interaction_manager = MediaInteractionManager()
