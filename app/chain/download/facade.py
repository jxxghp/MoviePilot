"""下载处理链稳定 Facade。"""

from collections.abc import Callable
from typing import Any, TypeVar

from app.chain.download.batch import DownloadBatchOwner
from app.chain.download.existence import DownloadExistenceOwner
from app.chain.download.failure import DownloadFailureOwner
from app.chain.download.history import DownloadHistoryOwner
from app.chain.download.processing import DownloadProcessingOwner
from app.chain.download.selection import DownloadSelectionOwner
from app.chain.download.submission import DownloadSubmissionOwner
from app.chain.download.subtitle import DownloadSubtitleOwner
from app.chain.download.tasks import DownloadTaskOwner
from app.runtime.events import Event, eventmanager
from app.schemas.types import EventType

_PUBLIC_MODULE = "app.chain.download"
_Handler = TypeVar("_Handler", bound=Callable[..., Any])


def _public_handler(handler: _Handler) -> _Handler:
    """在事件注册前恢复插件可见的稳定模块身份。"""
    handler.__module__ = _PUBLIC_MODULE
    return handler


class DownloadChain(
    DownloadSubtitleOwner,
    DownloadSelectionOwner,
    DownloadFailureOwner,
    DownloadSubmissionOwner,
    DownloadBatchOwner,
    DownloadExistenceOwner,
    DownloadTaskOwner,
    DownloadHistoryOwner,
    DownloadProcessingOwner,
):
    """组合下载选择、提交、结算、后处理与任务控制的稳定门面。"""

    __module__ = _PUBLIC_MODULE

    @eventmanager.register(EventType.DownloadFileDeleted)
    @_public_handler
    def download_file_deleted(self, event: Event) -> None:
        """通过稳定 DownloadChain 身份处理下载文件删除事件。"""
        self._download_file_deleted(event)
