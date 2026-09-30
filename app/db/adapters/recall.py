"""只读导出升级前的 Agent 快照，新会话消息不写入此适配器。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.messaging.recall import RecallLegacyRecord, RecallSession
from app.db.models.agentchat import AgentChat


class LegacyRecallRepository:
    """通过短会话导出有限旧快照，独立消息库保存可恢复的搬迁游标。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """注入主库工厂，只在尚未完成的历史搬迁时使用。"""
        self._factory = session_factory

    def page(self, user_id: str, after: int, limit: int) -> tuple[RecallLegacyRecord, ...]:
        """仅按 user_id 导出，时间沿用旧记录，不能伪装成升级时新发生的会话。"""
        with self._factory() as session:
            rows = session.scalars(select(AgentChat).where(AgentChat.user_id == user_id, AgentChat.id > after)
                                   .order_by(AgentChat.id).limit(limit)).all()
            return tuple(RecallLegacyRecord(
                row.id, RecallSession(row.session_id, source=row.source or 'chat', title=row.title or '',
                                      started_at=self._timestamp(row.created_at), last_active=self._timestamp(row.updated_at)),
                tuple(row.agent_messages or []),
            ) for row in rows)

    @staticmethod
    def _timestamp(value: str | None) -> float:
        """旧主库时间是宿主本地时间，无可用值时明确保留未知。"""
        if not value:
            return 0
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0
