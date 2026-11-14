"""
人工确认媒体身份的整理授权模型。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class TransferManualIdentityAuthorization(Base):
    """
    保存管理用户点击「智能助手整理」时签发的一次性身份修正授权。

    授权与失败历史、持久任务和结算版本绑定，消费一次即写回 used_at，
    因此预算门放行不会变成可循环的自动重试。
    """

    __tablename__ = "transfermanualidentityauthorization"

    id = get_id_column()
    history_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    settlement_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 签发入口，当前只有 history_ai_redo
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # 签发管理员名称快照
    issued_by: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    used_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 消费该授权的入口身份
    used_by: Mapped[Optional[str]] = mapped_column(String(128))
    media_source: Mapped[Optional[str]] = mapped_column(String(32))
    media_id: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index(
            "ix_transfermanualidentityauthorization_history",
            "history_id",
            "task_id",
        ),
        Index(
            "ix_transfermanualidentityauthorization_task",
            "task_id",
        ),
    )
