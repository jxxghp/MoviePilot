"""Agent 写工具调用的持久回执表。"""

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class AgentInvocation(Base):
    """保存一次写调用的稳定身份与结果状态，不保存敏感参数或原始结果。"""

    id = get_id_column()
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_token: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index(
            "ix_agentinvocation_identity",
            "principal_id", "session_id", "invocation_id",
            unique=True,
        ),
        Index("ix_agentinvocation_status_updated_id", "status", "updated_at", "id"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'pending', 'unknown')",
            name="ck_agentinvocation_status",
        ),
    )
