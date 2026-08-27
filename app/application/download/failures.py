"""下载失败冷却记录的类型化应用契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Union

from app.schemas.types import MediaSource


@dataclass(frozen=True, slots=True)
class DownloadFailureWrite:
    """一次下载失败冷却写入所需的完整稳定数据。"""

    fingerprint: str
    failed_at: str
    next_retry_at: str
    media_type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    site: Optional[int] = None
    site_name: Optional[str] = None
    torrent_id: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_size: Optional[Union[float, int]] = None
    downloader: Optional[str] = None
    source: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DownloadFailureSnapshot:
    """脱离数据库会话后供资源冷却判断使用的只读快照。"""

    fingerprint: str
    error_message: Optional[str]
    next_retry_at: Optional[str]


class DownloadFailureRepository(Protocol):
    """下载链读写失败冷却状态所需的最小持久化端口。"""

    def get_active_by_fingerprints(
        self,
        fingerprints: list[str],
        now_time: str,
    ) -> dict[str, DownloadFailureSnapshot]:
        """返回仍处于冷却期的不可变失败快照。"""
        ...

    def record_failure(self, failure: DownloadFailureWrite) -> None:
        """持久化一次失败事实，完成提交后不暴露数据库记录。"""
        ...
