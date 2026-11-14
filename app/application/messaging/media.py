"""
媒体搜索、下载与订阅交互的用户会话状态管理。
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.application.messaging.interaction import _ExpiringUserInteractionStore
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import NotificationChannel


@dataclass
class PendingMediaInteraction:
    """
    记录一次搜索/下载/订阅交互的当前上下文。

    :param request_id: 当前交互请求标识，用于校验按钮是否仍属于活动会话
    :param user_id: 所属用户标识，用于隔离不同用户的交互状态
    :param channel: 回复使用的通知渠道，None 表示未指定
    :param source: 渠道实例标识，None 表示未指定实例
    :param username: 用户显示名称，未知时为 None
    :param action: 本次媒体交互的业务动作，如搜索、下载或订阅
    :param keyword: 用户输入的媒体检索关键词
    :param phase: 当前阶段：media 选择媒体、torrent 选择资源、download-dir 选择目录
    :param page: 当前候选列表页索引，从 0 开始
    :param title: 当前交互视图的展示标题
    :param meta: 从输入解析出的媒体元信息，尚未识别时为 None
    :param current_media: 用户当前选定的影视或音乐信息，尚未选择时为 None
    :param items: 当前阶段供用户选择的候选对象
    :param download_dirs: 可供选择的下载目录列表
    :param pending_download_mode: 待目录选择完成后执行的下载模式，未等待下载时为 None
    :param pending_download_context: 等待目录选择的资源下载上下文，未暂存时为 None
    :param pending_no_exists: 待下载媒体的缺失集数映射，未提供时为 None
    :param pending_torrent_page: 进入目录选择前的种子列表页索引，返回时恢复此页
    :param created_at: 会话创建的本地时间，用于计算过期时间
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
    current_media: Optional[MediaInfo | MusicInfo] = None
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
