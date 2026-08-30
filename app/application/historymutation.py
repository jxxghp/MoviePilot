"""历史删除命令及其事务端口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from app.schemas.common import JsonData
from app.schemas.history import TransferHistoryDeleteResult, TransferHistoryDeleteStep


@dataclass(frozen=True, slots=True)
class HistoryMutationResult:
    """描述历史记录维护操作是否成功及兼容提示。"""

    success: bool
    message: str = ""


class DownloadHistoryMutationRepository(Protocol):
    """下载历史删除用例需要的最小持久化端口。"""

    def stage_delete_history(self, history_id: int) -> None:
        """暂存下载历史删除。"""
        ...


class TransferHistoryMutationRecord(Protocol):
    """整理历史删除用例读取的稳定字段投影。"""

    @property
    def transfer_task_id(self) -> Optional[str]:
        """返回持久整理任务标识。"""
        ...

    @property
    def src(self) -> Optional[str]:
        """返回源路径。"""
        ...

    @property
    def src_storage(self) -> Optional[str]:
        """返回源存储标识。"""
        ...

    @property
    def src_fileitem(self) -> Optional[JsonData]:
        """返回源文件项快照。"""
        ...

    @property
    def dest_fileitem(self) -> Optional[JsonData]:
        """返回目标文件项快照。"""
        ...

    @property
    def download_hash(self) -> Optional[str]:
        """返回下载任务 Hash。"""
        ...


class TransferHistoryMutationRepository(Protocol):
    """整理历史删除与清理用例需要的最小持久化端口。"""

    def get(self, history_id: int) -> Optional[TransferHistoryMutationRecord]:
        """读取整理历史。"""
        ...

    def stage_delete(self, history_id: int) -> None:
        """暂存整理历史删除。"""
        ...

    def stage_truncate(self) -> None:
        """暂存全部整理历史删除。"""
        ...


class DownloadFileMutationRepository(Protocol):
    """整理历史删除时关联下载文件状态更新端口。"""

    def stage_delete_file_by_fullpath(self, fullpath: str) -> None:
        """暂存下载文件删除状态。"""
        ...


class HistoryUnitOfWork(Protocol):
    """同步历史维护用例使用的事务端口。"""

    def commit(self) -> None:
        """提交当前事务。"""
        ...

    def rollback(self) -> None:
        """回滚当前事务。"""
        ...


class DownloadHistoryMutationCommand:
    """统一提交下载历史删除，避免 API 直接持有数据库事务。"""

    def __init__(
        self,
        *,
        repository: DownloadHistoryMutationRepository,
        unit_of_work: HistoryUnitOfWork,
    ) -> None:
        """保存下载历史持久化和事务端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def delete(self, history_id: int) -> HistoryMutationResult:
        """暂存并提交单条下载历史删除。"""
        self._repository.stage_delete_history(history_id)
        self._commit()
        return HistoryMutationResult(True)

    def _commit(self) -> None:
        """提交事务，失败时回滚。"""
        try:
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise


class TransferHistoryMutationCommand:
    """协调整理历史、关联文件状态和外部存储删除。"""

    def __init__(
        self,
        *,
        repository: TransferHistoryMutationRepository,
        download_repository: DownloadFileMutationRepository,
        unit_of_work: HistoryUnitOfWork,
        file_item_factory: Callable[[dict[str, JsonData]], Any],
        delete_media_file: Callable[[Any], bool],
        publish_download_file_deleted: Callable[[dict[str, Any]], None],
        clear_failures: Callable[[Optional[str], Optional[str]], None],
        file_exists: Optional[Callable[[Any], Optional[bool]]] = None,
    ) -> None:
        """保存历史事务、存储删除、事件和失败状态清理端口。"""
        self._repository = repository
        self._download_repository = download_repository
        self._unit_of_work = unit_of_work
        self._file_item_factory = file_item_factory
        self._file_exists = file_exists or (lambda _fileitem: True)
        self._delete_media_file = delete_media_file
        self._publish_download_file_deleted = publish_download_file_deleted
        self._clear_failures = clear_failures

    def delete(
        self,
        history_id: int,
        *,
        delete_source: bool = False,
        delete_destination: bool = False,
    ) -> TransferHistoryDeleteResult:
        """删除整理记录并返回源、目标文件及历史记录的分项结果。"""
        history = self._repository.get(history_id)
        if not history:
            return TransferHistoryDeleteResult(
                source=TransferHistoryDeleteStep(status="not_requested"),
                destination=TransferHistoryDeleteStep(status="not_requested"),
                history="not_found",
                message="记录不存在",
            )
        if history.transfer_task_id:
            return TransferHistoryDeleteResult(
                source=TransferHistoryDeleteStep(status="not_requested"),
                destination=TransferHistoryDeleteStep(status="not_requested"),
                history="retained",
                message="持久整理失败记录不可删除，请使用重试或人工复核入口",
            )

        destination_result = self._delete_file(
            history.dest_fileitem if delete_destination else None,
            requested=delete_destination,
            label="目标文件",
        )
        source_result = self._delete_file(
            history.src_fileitem if delete_source else None,
            requested=delete_source,
            label="源文件",
        )
        source_completed = source_result.status in {"deleted", "already_missing"}
        all_requested_completed = all(
            step.status in {"not_requested", "deleted", "already_missing"}
            for step in (source_result, destination_result)
        )

        if source_completed:
            source_payload = self._file_item_payload(history.src_fileitem)
            if source_payload is not None:
                source = self._file_item_factory(source_payload)
                self._download_repository.stage_delete_file_by_fullpath(
                    Path(source.path).as_posix()
                )

        if all_requested_completed:
            self._repository.stage_delete(history_id)

        if source_completed or all_requested_completed:
            self._commit()
        if source_completed:
            self._publish_download_file_deleted({
                "src": history.src,
                "hash": history.download_hash,
            })
        if all_requested_completed:
            self._clear_failures(history.src, history.src_storage)

        return TransferHistoryDeleteResult(
            source=source_result,
            destination=destination_result,
            history="deleted" if all_requested_completed else "retained",
            message=(
                "已删除整理记录"
                if all_requested_completed
                else next(
                    (
                        step.message
                        for step in (source_result, destination_result)
                        if step.status == "failed" and step.message
                    ),
                    "整理记录已保留，部分文件处理失败",
                )
            ),
        )

    def _delete_file(
        self,
        value: Optional[JsonData],
        *,
        requested: bool,
        label: str,
    ) -> TransferHistoryDeleteStep:
        """删除一个文件目标；原目标已不存在时按完成处理，失败不抛出以便继续汇总其他步骤。"""
        if not requested:
            return TransferHistoryDeleteStep(status="not_requested")
        payload = self._file_item_payload(value)
        if payload is None:
            return TransferHistoryDeleteStep(
                status="failed",
                message=f"{label}历史数据无效",
            )
        try:
            fileitem = self._file_item_factory(payload)
            if self._file_exists(fileitem) is False:
                return TransferHistoryDeleteStep(status="already_missing")
            if not self._delete_media_file(fileitem):
                return TransferHistoryDeleteStep(
                    status="failed",
                    message=f"{fileitem.path} 删除失败",
                )
        except Exception:
            return TransferHistoryDeleteStep(status="failed", message=f"{label}删除失败")
        return TransferHistoryDeleteStep(status="deleted")

    @staticmethod
    def _file_item_payload(value: JsonData) -> Optional[dict[str, JsonData]]:
        """仅接受可安全构造文件项的历史 JSON 对象。"""
        return value if isinstance(value, dict) else None

    def truncate(self) -> HistoryMutationResult:
        """在单一事务中清空旧历史，并保留当前失败任务记录。"""
        self._repository.stage_truncate()
        self._commit()
        return HistoryMutationResult(True, "已清空旧整理记录，失败任务记录已保留")

    def _commit(self) -> None:
        """提交历史事务，失败时回滚且不发布事件或清缓存。"""
        try:
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
