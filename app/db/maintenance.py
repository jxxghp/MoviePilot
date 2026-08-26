"""数据维护用例的 SQLAlchemy 适配器。"""

from typing import Any, Callable, ContextManager

from sqlalchemy import delete, exists, select

from app.db.base import execute_dml
from app.db.models.agentchat import AgentChat
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun
from app.db.models.downloadfailure import DownloadFailure
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.message import Message
from app.db.models.outbox import OutboxMessage
from app.db.models.siteuserdata import SiteUserData
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.transferhistory import TransferHistory
from app.db.uow import SqlAlchemyUnitOfWork


class DatabaseCleanupRepository:
    """把应用层清理端口映射到现有模型批量删除方法。"""

    def __init__(self, *, session_factory: Callable[[], ContextManager[Any]]) -> None:
        """保存会话工厂，使测试和不同数据库后端可以显式注入。"""
        self._session_factory = session_factory

    def session(self) -> ContextManager[Any]:
        """创建一次维护运行共用的数据库会话。"""
        return self._session_factory()

    @staticmethod
    def unit_of_work(db: Any) -> SqlAlchemyUnitOfWork:
        """把当前维护 Session 适配成显式批次事务边界。"""
        return SqlAlchemyUnitOfWork(db)

    @staticmethod
    def delete_messages(db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的消息。"""
        return Message.delete_before(db=db, before_time=cutoff, limit=limit)

    @staticmethod
    def delete_download_history(db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的下载历史。"""
        return DownloadHistory.delete_before(
            db=db,
            before_time=cutoff,
            limit=limit,
        )

    @staticmethod
    def delete_download_orphans(db: Any, limit: int) -> int:
        """删除已经失去父下载历史的文件记录。"""
        return DownloadFiles.delete_orphans(db=db, limit=limit)

    @staticmethod
    def delete_site_userdata(db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止日期的站点用户数据快照。"""
        return SiteUserData.delete_before(db=db, before_day=cutoff, limit=limit)

    @staticmethod
    def delete_transfer_history(db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的整理历史。"""
        return TransferHistory.delete_before(
            db=db,
            before_time=cutoff,
            limit=limit,
        )

    @staticmethod
    def delete_download_failures(db: Any, cutoff: str, limit: int) -> int:
        """删除已经过期的下载失败冷却记录。"""
        return DownloadFailure.delete_expired(
            db=db,
            before_time=cutoff,
            limit=limit,
        )

    @staticmethod
    def delete_subscribe_history(db: Any, cutoff: str, limit: int) -> int:
        """分批删除超过用户保留期的已完成订阅快照。"""
        return DatabaseCleanupRepository._delete_selected_ids(
            db=db,
            model=SubscribeHistory,
            condition=SubscribeHistory.date < cutoff,
            limit=limit,
        )

    @staticmethod
    def delete_agent_chats(db: Any, cutoff: str, limit: int) -> int:
        """清理旧会话，但保留仍被 Agent 定时任务引用的上下文。"""
        task_reference = exists(
            select(AgentTask.id).where(AgentTask.session_id == AgentChat.session_id)
        )
        return DatabaseCleanupRepository._delete_selected_ids(
            db=db,
            model=AgentChat,
            condition=(AgentChat.updated_at < cutoff) & ~task_reference,
            limit=limit,
        )

    @staticmethod
    def delete_agent_task_runs(db: Any, cutoff: str, limit: int) -> int:
        """清理旧终态运行，但保留运行中和父任务最后一次运行。"""
        latest_run_reference = exists(
            select(AgentTask.id).where(AgentTask.last_run_id == AgentTaskRun.run_id)
        )
        return DatabaseCleanupRepository._delete_selected_ids(
            db=db,
            model=AgentTaskRun,
            condition=(
                (AgentTaskRun.started_at < cutoff)
                & (AgentTaskRun.status != "running")
                & ~latest_run_reference
            ),
            limit=limit,
        )

    @staticmethod
    def delete_outbox_completed(db: Any, cutoff: str, limit: int) -> int:
        """删除过期 completed 记录，事务提交由维护用例统一控制。"""
        return DatabaseCleanupRepository._delete_outbox_status(
            db=db,
            status="completed",
            timestamp_column=OutboxMessage.completed_at,
            cutoff=cutoff,
            limit=limit,
        )

    @staticmethod
    def delete_outbox_dead(db: Any, cutoff: str, limit: int) -> int:
        """删除过期 dead-letter 记录，保留 pending/processing 恢复语义。"""
        return DatabaseCleanupRepository._delete_outbox_status(
            db=db,
            status="dead",
            timestamp_column=OutboxMessage.next_retry_at,
            cutoff=cutoff,
            limit=limit,
        )

    @staticmethod
    def _delete_outbox_status(
        *,
        db: Any,
        status: str,
        timestamp_column: Any,
        cutoff: str,
        limit: int,
    ) -> int:
        """先锁定有限 ID 再删除，避免一次维护事务无界膨胀。"""
        message_ids = db.execute(
            select(OutboxMessage.id)
            .where(
                OutboxMessage.status == status,
                timestamp_column.is_not(None),
                timestamp_column < cutoff,
            )
            .order_by(OutboxMessage.id)
            .limit(limit)
        ).scalars().all()
        if not message_ids:
            return 0
        return execute_dml(
            db,
            delete(OutboxMessage).where(OutboxMessage.id.in_(message_ids)),
            execution_options={"synchronize_session": False},
        )

    @staticmethod
    def _delete_selected_ids(
        *,
        db: Any,
        model: Any,
        condition: Any,
        limit: int,
    ) -> int:
        """按安全谓词锁定有限主键并暂存删除。"""
        record_ids = db.execute(
            select(model.id)
            .where(condition)
            .order_by(model.id)
            .limit(limit)
        ).scalars().all()
        if not record_ids:
            return 0
        return execute_dml(
            db,
            delete(model).where(model.id.in_(record_ids)),
            execution_options={"synchronize_session": False},
        )
