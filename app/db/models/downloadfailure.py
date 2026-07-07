from typing import List, Optional

from sqlalchemy import Column, Float, Index, Integer, String
from sqlalchemy.orm import Session

from app.db import Base, db_query, db_update, get_id_column


class DownloadFailure(Base):
    """
    下载失败冷却记录。
    """

    id = get_id_column()
    # 资源失败指纹
    fingerprint = Column(String, nullable=False)
    # 类型 电影/电视剧
    type = Column(String)
    # 标题
    title = Column(String)
    # 年份
    year = Column(String)
    # TMDBID
    tmdbid = Column(Integer)
    # 豆瓣ID
    doubanid = Column(String)
    # Sxx
    seasons = Column(String)
    # Exx
    episodes = Column(String)
    # 站点ID
    site = Column(Integer)
    # 站点名称
    site_name = Column(String)
    # 种子资源键
    torrent_id = Column(String)
    # 种子名称
    torrent_name = Column(String)
    # 种子大小
    torrent_size = Column(Float)
    # 下载器
    downloader = Column(String)
    # 下载来源
    source = Column(String)
    # 失败原因
    error_message = Column(String)
    # 重试次数
    retry_count = Column(Integer, default=0)
    # 首次失败时间
    first_failed_at = Column(String)
    # 最近失败时间
    last_failed_at = Column(String)
    # 下次允许重试时间
    next_retry_at = Column(String)

    __table_args__ = (
        Index("ux_downloadfailure_fingerprint", "fingerprint", unique=True),
        Index("ix_downloadfailure_next_retry_at", "next_retry_at"),
        Index("ix_downloadfailure_media_site", "type", "tmdbid", "doubanid", "site"),
    )

    @classmethod
    @db_query
    def get_active_by_fingerprints(
            cls,
            db: Session,
            fingerprints: List[str],
            now_time: str,
    ) -> List["DownloadFailure"]:
        """
        按指纹批量查询仍处于冷却期的失败记录。
        """
        normalized = list(dict.fromkeys([fingerprint for fingerprint in fingerprints if fingerprint]))
        if not normalized:
            return []
        return (
            db.query(cls)
            .filter(cls.fingerprint.in_(normalized), cls.next_retry_at > now_time)
            .all()
        )

    @classmethod
    @db_update
    def record_failure(
            cls,
            db: Session,
            fingerprint: str,
            now_time: str,
            next_retry_at: str,
            **kwargs: object,
    ) -> "DownloadFailure":
        """
        新增或更新资源失败记录。
        """
        failure = db.query(cls).filter(cls.fingerprint == fingerprint).first()
        payload = {
            **kwargs,
            "fingerprint": fingerprint,
            "last_failed_at": now_time,
            "next_retry_at": next_retry_at,
        }
        if failure:
            payload["retry_count"] = (failure.retry_count or 0) + 1
            for key, value in payload.items():
                setattr(failure, key, value)
            return failure

        failure = cls(
            **payload,
            retry_count=1,
            first_failed_at=now_time,
        )
        db.add(failure)
        return failure

    @classmethod
    @db_update
    def delete_expired(
            cls,
            db: Session,
            before_time: str,
            limit: Optional[int] = 500,
    ) -> int:
        """
        分批清理已过期较久的失败冷却记录。
        """
        ids = [
            row[0]
            for row in db.query(cls.id)
            .filter(cls.next_retry_at < before_time)
            .order_by(cls.id.asc())
            .limit(limit)
            .all()
        ]
        if not ids:
            return 0
        return db.query(cls).filter(cls.id.in_(ids)).delete(synchronize_session=False)
