"""自动订阅搜索任务的逐页检查点。"""

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class SearchSession(Base):
    """一条订阅搜索任务一行；保存不含凭据的页快照及 CAS 版本。"""

    id = get_id_column()
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", name="uq_searchsession_task_id"),)
