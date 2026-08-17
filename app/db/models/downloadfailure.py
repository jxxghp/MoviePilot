from typing import List, Optional

from sqlalchemy import Float, Index, Integer, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import db_query, db_update
from app.db.models._constraints import media_identity_constraint


class DownloadFailure(Base):
    """
    下载失败冷却记录。
    """

    id = get_id_column()
    # 资源失败指纹
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    # 类型 电影/电视剧
    type: Mapped[Optional[str]] = mapped_column(String)
    # 标题
    title: Mapped[Optional[str]] = mapped_column(String)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 媒体数据源与原生ID
    media_source: Mapped[Optional[str]] = mapped_column(String)
    media_id: Mapped[Optional[str]] = mapped_column(String)
    # Sxx
    seasons: Mapped[Optional[str]] = mapped_column(String)
    # Exx
    episodes: Mapped[Optional[str]] = mapped_column(String)
    # 站点ID
    site: Mapped[Optional[int]] = mapped_column(Integer)
    # 站点名称
    site_name: Mapped[Optional[str]] = mapped_column(String)
    # 种子资源键
    torrent_id: Mapped[Optional[str]] = mapped_column(String)
    # 种子名称
    torrent_name: Mapped[Optional[str]] = mapped_column(String)
    # 种子大小
    torrent_size: Mapped[Optional[float]] = mapped_column(Float)
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 下载来源
    source: Mapped[Optional[str]] = mapped_column(String)
    # 失败原因
    error_message: Mapped[Optional[str]] = mapped_column(String)
    # 重试次数
    retry_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 首次失败时间
    first_failed_at: Mapped[Optional[str]] = mapped_column(String)
    # 最近失败时间
    last_failed_at: Mapped[Optional[str]] = mapped_column(String)
    # 下次允许重试时间
    next_retry_at: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("downloadfailure"),
        Index("ux_downloadfailure_fingerprint", "fingerprint", unique=True),
        Index("ix_downloadfailure_next_retry_at", "next_retry_at"),
        Index("ix_downloadfailure_media_identity_site", "type", "media_source", "media_id", "site"),
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
        return list(db.execute(
            select(cls)
            .where(cls.fingerprint.in_(normalized), cls.next_retry_at > now_time)
        ).scalars().all())

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
        failure = db.execute(
            select(cls).where(cls.fingerprint == fingerprint)
        ).scalars().first()
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
        ids = db.execute(
            select(cls.id)
            .where(cls.next_retry_at < before_time)
            .order_by(cls.id.asc())
            .limit(limit)
        ).scalars().all()
        if not ids:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
