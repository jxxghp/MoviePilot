"""历史、媒体服务器与 Dashboard 查询依赖。"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.context import get_async_session, get_host_runtime, get_sync_session
from app.application.dashboard import DashboardQueryService
from app.application.history import (
    DownloadHistoryMutationCommand,
    HistoryQueryService,
    TransferHistoryLookupService,
    TransferHistoryMutationCommand,
    clear_transfer_failures,
)
from app.application.mediaserver import MediaServerQueryService
from app.application.orchestration.storage import StorageChain
from app.runtime.events import eventmanager
from app.schemas.types import EventType
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.startup.ports.context import HostRuntime


def get_mediaserver_query_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> MediaServerQueryService:
    """组装媒体服务器本地条目异步查询服务。"""
    return MediaServerQueryService(
        repository=runtime.history.media_server_repository(db)
    )


def get_dashboard_query_service(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> DashboardQueryService:
    """组装 Dashboard 媒体与整理历史统计查询服务。"""
    from app.application.orchestration.dashboard import DashboardChain

    return DashboardQueryService(
        repository=runtime.history.transfer_repository(db),
        media_statistics=DashboardChain().media_statistic,
    )


def get_download_history_mutation_command(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> DownloadHistoryMutationCommand:
    """组装下载历史删除用例及其请求级事务。"""
    return DownloadHistoryMutationCommand(
        repository=runtime.history.download_repository(db),
        unit_of_work=runtime.persistence.sync_transaction(db),
    )


def get_history_query_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> HistoryQueryService:
    """组装历史列表和详情异步查询服务。"""
    return HistoryQueryService(
        download_repository=runtime.history.download_repository(db),
        transfer_repository=runtime.history.transfer_repository(db),
    )


def get_transfer_history_lookup_service(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> TransferHistoryLookupService:
    """组装手动整理使用的同步历史投影服务。"""
    return TransferHistoryLookupService(
        runtime.history.transfer_repository(db)
    )


def get_transfer_history_mutation_command(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> TransferHistoryMutationCommand:
    """组装整理历史删除、文件处理和事件发布用例。"""
    storage_chain = StorageChain()
    return TransferHistoryMutationCommand(
        repository=runtime.history.transfer_repository(db),
        download_repository=runtime.history.download_repository(db),
        unit_of_work=runtime.persistence.sync_transaction(db),
        file_item_factory=lambda payload: _SchemaFileItem(**payload),
        delete_media_file=storage_chain.delete_media_file,
        publish_download_file_deleted=lambda payload: eventmanager.send_event(
            EventType.DownloadFileDeleted,
            payload,
        ),
        clear_failures=clear_transfer_failures,
    )
