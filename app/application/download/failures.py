"""
下载失败冷却记录的类型化应用契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Union

from app.schemas.types import MediaSource


@dataclass(frozen=True, slots=True)
class DownloadFailureWrite:
    """
    一次下载失败冷却写入所需的完整稳定数据。

    :param fingerprint: 按资源身份构造的稳定指纹，用于关联失败冷却
    :param failed_at: 本次下载失败的时间文本
    :param next_retry_at: 冷却结束后最早可再次尝试的时间文本
    :param media_type: 下载目标的媒体类型
    :param title: 识别出的标准媒体标题
    :param year: 媒体发行年份；未知时为 None
    :param media_source: 标准媒体来源，与 media_id 配对使用
    :param media_id: 当前来源中的原生媒体标识
    :param seasons: 已识别的季号或季范围文本
    :param episodes: 识别出的集号或集范围文本
    :param site: 种子所属站点主键
    :param site_name: 种子所属站点名称
    :param torrent_id: 站点内的种子标识
    :param torrent_name: 种子原始标题
    :param torrent_size: 种子总大小，单位为字节
    :param downloader: 下载器配置名称
    :param source: 触发本次下载的业务入口
    :param error_message: 下载失败的可读错误摘要
    """

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
    """
    脱离数据库会话后供资源冷却判断使用的只读快照。

    :param fingerprint: 按资源身份构造的稳定指纹，用于关联失败冷却
    :param error_message: 下载失败的可读错误摘要
    :param next_retry_at: 冷却结束后最早可再次尝试的时间文本
    """

    fingerprint: str
    error_message: Optional[str]
    next_retry_at: Optional[str]


class DownloadFailureRepository(Protocol):
    """
    下载链读写失败冷却状态所需的最小持久化端口。
    """

    def get_active_by_fingerprints(
        self,
        fingerprints: list[str],
        now_time: str,
    ) -> dict[str, DownloadFailureSnapshot]:
        """
        返回仍处于冷却期的不可变失败快照。

        :param fingerprints: 待检查冷却状态的资源指纹列表
        :param now_time: 本次判断使用的当前时间文本
        :return: 以资源指纹为键的冷却快照；没有记录或已过冷却期的资源不写入映射
        """
        ...

    def record_failure(self, failure: DownloadFailureWrite) -> None:
        """
        持久化一次失败事实，完成提交后不暴露数据库记录。

        :param failure: 本次下载失败与下一次重试时间的稳定快照
        """
        ...
