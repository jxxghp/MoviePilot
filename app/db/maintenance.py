"""数据维护用例的 SQLAlchemy 适配器。"""

from typing import Any, Callable, ContextManager

from app.db.models.downloadfailure import DownloadFailure
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.message import Message
from app.db.models.siteuserdata import SiteUserData
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
