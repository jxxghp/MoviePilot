import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Union

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import MessageChannel


@dataclass
class PendingMediaInteraction:
    """
    记录一次搜索/下载/订阅交互的当前上下文。
    """

    request_id: str
    user_id: str
    channel: Optional[MessageChannel]
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


class MediaInteractionManager:
    """
    管理用户当前激活的媒体交互状态。

    每个用户只保留一个有效会话，避免旧按钮与新一轮搜索混用。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        """初始化按请求和用户索引的媒体会话表。"""
        self._by_id: Dict[str, PendingMediaInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """
        清理超时会话，避免内存中残留旧交互状态。
        """
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
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
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)

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
            self._by_id[request.request_id] = request
            self._by_user[user_key] = request.request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingMediaInteraction]:
        """
        按用户读取当前会话，供文本回复和旧按钮兼容使用。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingMediaInteraction]:
        """
        按请求 ID 读取会话，并校验用户归属。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        """
        主动结束一条会话。
        """
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self) -> None:
        """
        清空所有交互状态，主要用于测试。
        """
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


media_interaction_manager = MediaInteractionManager()
