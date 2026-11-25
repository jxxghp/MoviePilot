"""
整理历史的隔离仓储工厂；不注册测试用例。
"""

from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
from app.db.session import SessionFactory, async_session_scope


def _repository() -> TransactionalTransferHistoryRepository:
    """
    构造类型化整理历史仓储。
    """
    return TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
