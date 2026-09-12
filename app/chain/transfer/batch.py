"""目录整理批次的持久化准入与执行编排。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.application.transfer.workflow import (
    TransferAdmission,
    TransferPlanningInput,
    TransferTask,
)
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.exception import OperationInterrupted
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaSource, MediaType

from .request import _TransferSubmissionCollector


@dataclass(slots=True)
class TransferBatchRun:
    """一次目录批次准入、构建和执行所需的稳定请求上下文。"""

    root_fileitem: FileItem
    file_items: List[Tuple[FileItem, bool]]
    inherited_meta_map: Dict[Tuple[str, str], MetaBase]
    build_file_meta: Callable[[Path, Optional[List[str]]], Optional[MetaBase]]
    meta: Optional[MetaBase]
    mediainfo: Optional[Union[MediaInfo, MusicInfo]]
    media_source: Optional[MediaSource]
    media_id: Optional[str]
    batch_mtype: Optional[MediaType]
    target_directory: Optional[TransferDirectoryConf]
    target_storage: Optional[str]
    target_path: Optional[Path]
    transfer_type: Optional[str]
    scrape: Optional[bool]
    library_type_folder: Optional[bool]
    library_category_folder: Optional[bool]
    downloader: Optional[str]
    download_hash: Optional[str]
    transfer_batch_id: str
    transfer_batch_title: str
    transfer_batch_root: str
    requested_batch_total: Optional[int]
    recovery_options: dict[str, Any]
    manual: bool
    background: bool
    preview: bool
    reorganize: bool
    force: bool
    continue_callback: Optional[Callable[[], bool]]
    cleanup_dest_fileitem: Optional[FileItem]
    recovery_admission: Optional[TransferAdmission]
    music_release_regions: Optional[list[str]]
    music_release_scripts: Optional[list[str]]
    selected_music_track_map: dict[str, MusicInfo]
    submission: _TransferSubmissionCollector


class TransferBatchMixin(_TransferOwnerBase):
    """在昂贵识别前持久化完整候选集，并统一批次执行收口。"""

    def _run_transfer_batch(
        self, request: TransferBatchRun
    ) -> tuple[bool, Union[str, dict[str, Any]]]:
        """准入完整批次、构建任务并返回统一提交回执。"""
        planned_file_count = len(request.file_items)
        recovered_total = (
            request.recovery_options.get("transfer_batch_total")
            or request.requested_batch_total
        )
        batch_total = (
            recovered_total
            if isinstance(recovered_total, int) and recovered_total > 0
            else planned_file_count
        )
        action = "预览" if request.preview else "计划整理"
        logger.info("正在%s %s 个文件...", action, planned_file_count)
        request.submission.expect(request.file_items)
        preadmissions: dict[tuple[str, str], TransferAdmission] = {}
        if (
            request.background
            and not request.preview
            and request.recovery_admission is None
            and planned_file_count > 1
        ):
            try:
                preadmissions = self._preadmit_transfer_batch(request, batch_total)
                logger.info(
                    "整理批次已完整登记：%s，共 %s 个文件",
                    request.transfer_batch_id,
                    len(preadmissions),
                )
            except Exception as error:
                logger.error("完整登记整理批次失败：%s", error, exc_info=True)
                message = f"整理批次登记失败：{error}"
                for candidate, _ in request.file_items:
                    request.submission.record(candidate, "failed", message)
                return request.submission.result(
                    False, [message], preview=False, preview_items=[]
                )
        try:
            tasks, success, errors, skipped_count, skipped_torrents = (
                self._build_transfer_tasks(
                    file_items=request.file_items,
                    inherited_meta_map=request.inherited_meta_map,
                    build_file_meta=request.build_file_meta,
                    meta=request.meta,
                    mediainfo=request.mediainfo,
                    media_source=request.media_source,
                    media_id=request.media_id,
                    batch_mtype=request.batch_mtype,
                    target_directory=request.target_directory,
                    target_storage=request.target_storage,
                    target_path=request.target_path,
                    transfer_type=request.transfer_type,
                    scrape=request.scrape,
                    library_type_folder=request.library_type_folder,
                    library_category_folder=request.library_category_folder,
                    downloader=request.downloader,
                    download_hash=request.download_hash,
                    transfer_batch_id=request.transfer_batch_id,
                    transfer_batch_title=request.transfer_batch_title,
                    transfer_batch_root=request.transfer_batch_root,
                    transfer_batch_total=batch_total,
                    manual=request.manual,
                    background=request.background,
                    preview=request.preview,
                    reorganize=request.reorganize,
                    force=request.force,
                    continue_callback=request.continue_callback,
                    cleanup_dest_fileitem=request.cleanup_dest_fileitem,
                    recovery_admission=request.recovery_admission,
                    preadmissions=preadmissions,
                    music_release_regions=request.music_release_regions,
                    music_release_scripts=request.music_release_scripts,
                    selected_music_track_map=request.selected_music_track_map,
                    submission=request.submission,
                )
            )
        except OperationInterrupted:
            return request.submission.result(
                False,
                [f"{request.root_fileitem.name} 已取消"],
                preview=False,
                preview_items=[],
            )
        success, errors, preview_items = self._execute_transfer_tasks(
            transfer_tasks=tasks,
            preview=request.preview,
            continue_callback=request.continue_callback,
            all_success=success,
            err_msgs=errors,
            submission=request.submission,
        )
        if skipped_count == planned_file_count and skipped_torrents:
            for skipped_hash, skipped_downloader in skipped_torrents:
                logger.info("补充设置下载任务已整理标签：%s", skipped_hash)
                self._TransferChain__mark_torrent_completed_if_done(
                    skipped_hash, skipped_downloader
                )
        return request.submission.result(
            success,
            errors,
            preview=request.preview,
            preview_items=preview_items,
        )

    def _preadmit_transfer_batch(
        self,
        request: TransferBatchRun,
        batch_total: int,
    ) -> dict[tuple[str, str], TransferAdmission]:
        """先原子登记完整候选集，再允许任何联网识别或内存排队发生。"""
        admission_items: list[tuple[str, str, TransferPlanningInput]] = []
        for index, (file_item, _) in enumerate(request.file_items):
            file_storage, file_path = file_item.storage, file_item.path
            if not file_storage or not file_path:
                raise ValueError("整理候选缺少存储或源路径")
            selected_track = None
            if file_storage == "local" and self._is_audio_file(file_item):
                selected_track = request.selected_music_track_map.get(
                    str(Path(file_path).resolve())
                )
            raw_task = TransferTask(
                fileitem=file_item,
                meta=request.meta if len(request.file_items) == 1 else None,
                mediainfo=selected_track or (
                    request.mediainfo if len(request.file_items) == 1 else None
                ),
                media_source=request.media_source,
                media_id=request.media_id,
                mtype=request.batch_mtype,
                target_directory=request.target_directory,
                target_storage=request.target_storage,
                target_path=request.target_path,
                transfer_type=request.transfer_type,
                scrape=request.scrape,
                library_type_folder=request.library_type_folder,
                library_category_folder=request.library_category_folder,
                downloader=request.downloader,
                download_hash=request.download_hash,
                transfer_batch_id=request.transfer_batch_id,
                transfer_batch_title=request.transfer_batch_title,
                transfer_batch_root=request.transfer_batch_root,
                transfer_batch_total=batch_total,
                music_release_regions=request.music_release_regions,
                music_release_scripts=request.music_release_scripts,
                manual=request.manual,
                background=True,
                preview=False,
            )
            planning_input = self._TransferChain__build_planning_input(
                raw_task,
                cleanup_dest_fileitem=(
                    request.cleanup_dest_fileitem if index == 0 else None
                ),
            )
            admission_items.append((file_storage, file_path, planning_input))
        admissions = self._transfer_admissions.admit_batch(
            items=admission_items,
            replace_inactive=request.manual,
        )
        return {(item.storage, item.src_path): item for item in admissions}

    def _bind_batch_admission(
        self,
        task: TransferTask,
        recovery: Optional[TransferAdmission],
        preadmissions: dict[tuple[str, str], TransferAdmission],
    ) -> None:
        """把恢复记录或批次预登记记录绑定到最终任务。"""
        fileitem = task.fileitem
        if recovery and (
            fileitem.storage == recovery.storage and fileitem.path == recovery.src_path
        ):
            task.bind_admission_task_id(recovery.task_id)
            self._TransferChain__bind_claimed_admission(task, recovery)
            if recovery.planning_input:
                task.bind_planning_input(recovery.planning_input)
            if recovery.checkpoint:
                task.bind_plan_checkpoint(recovery.checkpoint)
        preadmission = preadmissions.get((fileitem.storage or "", fileitem.path or ""))
        if preadmission is not None:
            task.bind_admission_task_id(preadmission.task_id)
            task.bind_planning_input(preadmission.planning_input)
