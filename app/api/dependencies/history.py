"""历史、媒体服务器与 Dashboard 查询依赖。"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.data import get_async_db, get_db
from app.api.dependencies.data import repository, transaction
from app.application.dashboard import DashboardQueryService
from app.application.history import (
    DownloadHistoryMutationCommand,
    HistoryQueryService,
    TransferHistoryLookupService,
    TransferHistoryMutationCommand,
    clear_transfer_failures,
)
from app.application.mediaserver import MediaServerQueryService
from app.chain.storage import StorageChain
from app.runtime.events import eventmanager
from app.schemas.types import EventType
from app.schemas.workflow import FileItem as _SchemaFileItem


def get_mediaserver_query_service(
    db: AsyncSession = Depends(get_async_db),
) -> MediaServerQueryService:
    """组装媒体服务器本地条目异步查询服务。"""
    return MediaServerQueryService(repository=repository("media_server", db))


def get_dashboard_query_service(
    db: Session = Depends(get_db),
) -> DashboardQueryService:
    """组装 Dashboard 媒体与整理历史统计查询服务。"""
    from app.chain.dashboard import DashboardChain

    return DashboardQueryService(
        repository=repository("transfer_history", db),
        media_statistics=DashboardChain().media_statistic,
    )


def get_download_history_mutation_command(
    db: Session = Depends(get_db),
) -> DownloadHistoryMutationCommand:
    """组装下载历史删除用例及其请求级事务。"""
    return DownloadHistoryMutationCommand(
        repository=repository("download_history", db),
        unit_of_work=transaction("sync", db),
    )


def get_history_query_service(
    db: AsyncSession = Depends(get_async_db),
) -> HistoryQueryService:
    """组装历史列表和详情异步查询服务。"""
    return HistoryQueryService(
        download_repository=repository("download_history", db),
        transfer_repository=repository("transfer_history", db),
    )


def get_transfer_history_lookup_service(
    db: Session = Depends(get_db),
) -> TransferHistoryLookupService:
    """组装手动整理使用的同步历史投影服务。"""
    return TransferHistoryLookupService(repository("transfer_history", db))


def get_transfer_history_mutation_command(
    db: Session = Depends(get_db),
) -> TransferHistoryMutationCommand:
    """组装整理历史删除、文件处理和事件发布用例。"""
    storage_chain = StorageChain()
    return TransferHistoryMutationCommand(
        repository=repository("transfer_history", db),
        download_repository=repository("download_history", db),
        unit_of_work=transaction("sync", db),
        file_item_factory=lambda payload: _SchemaFileItem(**payload),
        delete_media_file=storage_chain.delete_media_file,
        publish_download_file_deleted=lambda payload: eventmanager.send_event(
            EventType.DownloadFileDeleted,
            payload,
        ),
        clear_failures=clear_transfer_failures,
    )
