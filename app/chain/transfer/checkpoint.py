"""整理 Chain 的冻结检查点构建与任务恢复。"""

from pathlib import Path
from typing import Any, Callable, Optional, Union, cast

from app.application.classification.reference import (
    EffectiveClassificationSnapshot,
    apply_persisted_classification_snapshot,
)
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferTask,
)
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode

MetaSnapshotRestorer = Callable[[Optional[dict[str, Any]], object], MetaBase]
MediaSnapshotRestorer = Callable[
    [Optional[dict[str, Any]], object],
    Union[MediaInfo, MusicInfo],
]


def _apply_classification_snapshot(
    media: Union[MediaInfo, MusicInfo],
    snapshot: EffectiveClassificationSnapshot,
) -> Union[MediaInfo, MusicInfo]:
    """把持久分类标量恢复到媒体副本，不重新执行活动策略。"""
    return cast(
        Union[MediaInfo, MusicInfo],
        apply_persisted_classification_snapshot(media, snapshot),
    )


def build_planning_rejection_checkpoint(
    task: TransferTask,
    *,
    error: str,
    planning_input: TransferPlanningInput,
    classification_snapshot: EffectiveClassificationSnapshot,
) -> TransferPlanCheckpoint:
    """把确定性的宿主规划错误冻结为可重放的零步骤失败计划。"""
    meta_kind = planning_input.options.get("_meta_kind")
    mediainfo_kind = planning_input.options.get("_mediainfo_kind")
    source_path = task.fileitem.path
    source_storage = task.fileitem.storage
    if not source_path or not source_storage:
        raise TransferPlanningStateError("整理拒绝计划缺少源文件存储或路径")
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage=source_storage,
        root_target_path=source_path,
        final_target_path=source_path,
        resolved_transfer_type=(
            task.transfer_type or planning_input.requested_transfer_type or "copy"
        ),
        items=(),
        classification_snapshot=classification_snapshot,
        resolved_meta=planning_input.meta,
        resolved_meta_kind=meta_kind if isinstance(meta_kind, str) else None,
        resolved_mediainfo=planning_input.mediainfo,
        resolved_mediainfo_kind=(
            mediainfo_kind if isinstance(mediainfo_kind, str) else None
        ),
        resolved_episodes_info=planning_input.episodes_info,
        need_notify=planning_input.need_notify,
        overwrite_mode=planning_input.overwrite_mode,
        rejection_error=error,
    )


def restore_planned_task(
    task: TransferTask,
    *,
    restore_meta: MetaSnapshotRestorer,
    restore_media: MediaSnapshotRestorer,
) -> None:
    """用冻结检查点覆盖易受配置和在线识别变化影响的任务字段。"""
    checkpoint = task.plan_checkpoint
    if checkpoint is None:
        raise TransferPlanningStateError("planned 任务缺少整理计划检查点")
    if checkpoint.provider_invocation is not None:
        invocation = checkpoint.provider_invocation
        task.fileitem = FileItem.model_validate(invocation.fileitem)
        task.meta = restore_meta(invocation.meta, invocation.meta_kind)
        restored_media = restore_media(invocation.mediainfo, invocation.mediainfo_kind)
        task.mediainfo = _apply_classification_snapshot(
            restored_media,
            checkpoint.classification_snapshot,
        )
        task.target_directory = (
            TransferDirectoryConf.model_validate(invocation.target_directory)
            if invocation.target_directory
            else None
        )
        task.target_storage = invocation.target_storage
        task.target_path = Path(invocation.target_path) if invocation.target_path else None
        task.transfer_type = invocation.transfer_type
        task.scrape = invocation.scrape
        task.library_type_folder = invocation.library_type_folder
        task.library_category_folder = invocation.library_category_folder
        task.episodes_info = [
            TmdbEpisode.model_validate(item) for item in invocation.episodes_info
        ]
        task.mark_planning_context_restored()
        return
    if checkpoint.resolved_meta:
        task.meta = restore_meta(
            checkpoint.resolved_meta,
            checkpoint.resolved_meta_kind,
        )
    elif task.meta is None and checkpoint.rejection_error is None:
        raise TransferPlanningStateError("整理计划检查点缺少已解析元数据")
    if checkpoint.resolved_mediainfo:
        restored_media = restore_media(
            checkpoint.resolved_mediainfo,
            checkpoint.resolved_mediainfo_kind,
        )
        task.mediainfo = _apply_classification_snapshot(
            restored_media,
            checkpoint.classification_snapshot,
        )
    elif task.mediainfo is None and checkpoint.rejection_error is None:
        raise TransferPlanningStateError("整理计划检查点缺少已识别媒体信息")
    task.episodes_info = [
        TmdbEpisode.model_validate(item) for item in checkpoint.resolved_episodes_info
    ]
    task.target_storage = checkpoint.target_storage
    task.target_path = Path(checkpoint.root_target_path)
    task.transfer_type = checkpoint.resolved_transfer_type
    task.scrape = checkpoint.need_scrape
    task.mark_planning_context_restored()
