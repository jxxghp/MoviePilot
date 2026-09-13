"""艺术家完整作品获取任务模型。"""

from typing import Any, Optional

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class MusicArtistAcquisition(Base):
    """保存“合集基线 + 缺失补齐”的一次聚合任务。"""

    id = get_id_column()
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_key: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    artist_source: Mapped[str] = mapped_column(String(64), nullable=False)
    artist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artist_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    covered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supplement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    finished_at: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_musicartistacquisition_job_id", "job_id", unique=True),
        Index(
            "ix_musicartistacquisition_plan_identity",
            "username",
            "plan_key",
            unique=True,
        ),
        Index(
            "ix_musicartistacquisition_artist_updated",
            "artist_source",
            "artist_id",
            "updated_at",
            "id",
        ),
    )
