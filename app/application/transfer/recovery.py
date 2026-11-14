"""
整理任务的只读 AI 接管校验、失败证据恢复和历史解绑用例。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionRepository,
    TransferExecutionState,
    TransferFailureDiscardResult,
)
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType

if TYPE_CHECKING:
    from app.application.transfer.models import TransferPlanCheckpoint, TransferPlanningInput


def ai_takeover_rejection(
        *, history_id: int, task_id: Optional[str], repository: TransferExecutionRepository,
) -> Optional[str]:
    """
    只读核对 AI 接管入口的任务归属，不提前登记普通重试或清理证据。

    :param history_id: 请求接管的历史标识
    :param task_id: 历史关联任务；旧历史为 None
    :param repository: 读取最新持久执行状态的端口
    :return: 不允许接管的原因；允许时为 None
    """
    if not task_id:
        return None
    snapshot = repository.get_snapshot(task_id=task_id)
    if snapshot is None or snapshot.terminal_history_id != history_id:
        return "整理任务与历史状态已变化，请刷新后重试"
    if snapshot.state is TransferExecutionState.MANUAL_REVIEW:
        return "这条整理任务需要先完成人工确认，再交给智能助手"
    if snapshot.state is not TransferExecutionState.FAILED:
        return "整理任务正在处理或等待重试，请等待当前任务结束"
    return None


def music_replanning_input(checkpoint: TransferPlanCheckpoint) -> Optional[TransferPlanningInput]:
    """
    为零文件副作用的音乐拒绝重建输入，显式身份保留，仅刷新派生识别或分类。
    """
    planning = checkpoint.planning_input
    if (not checkpoint.rejection_error or checkpoint.items or checkpoint.provider_invocation
            or checkpoint.legacy_transfer_providers or checkpoint.resolved_meta_kind != "MetaMusic"):
        return None
    options = dict(planning.options)
    if not options.get("music_recognition_scope"):
        if planning.media_id and planning.mediainfo:
            options["music_replan_classification"] = True
        else:
            source = planning.source_fileitem
            path = Path(str(source.get("path") or ""))
            if not path.is_absolute() or path.suffix.lower() in {".cue", ".lrc", ".txt", ".yaml"}:
                return None
            storage = source.get("storage") or "local"
            if storage == "local":
                root = path.parent.parent if MetaMusic.parse_disc_dir(path.parent.name) else path.parent
                options["music_recognition_scope"] = {
                    "storage": storage, "directory": str(root), "main": str(path.relative_to(root)),
                    "files": [str(path.relative_to(root))], "regions": None, "scripts": None,
                }
            else:
                options["music_recognition_scope"] = {"storage": storage, "name_only": True}
    return replace(planning, media_type=MediaType.MUSIC.value, options=options)


class TransferRecoveryCommand:
    """
    把损坏任务恢复交给持久层原子清理，保留历史供重新规划。
    """

    def __init__(self, repository: TransferExecutionRepository) -> None:
        """
        保存拥有租约校验与事务的执行仓储。
        """
        self._repository = repository

    def discard_corrupt_task(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: str,
    ) -> bool:
        """
        以当前租约清理损坏任务，避免恢复线程再次回放旧步骤。
        """
        if not task_id or not lease_token or not error:
            raise ValueError("损坏任务收口缺少任务、租约或错误原因")
        return self._repository.discard_corrupt_task(
            task_id=task_id, lease_token=lease_token, error=error
        )

    def discard_corrupt_by_history(
            self,
            *,
            task_id: str,
            history_id: int,
    ) -> TransferFailureDiscardResult:
        """
        放弃无法重试的损坏任务，保留历史记录供重新生成计划。
        """
        if not task_id or history_id <= 0:
            raise ValueError("放弃损坏任务缺少任务或历史")
        return self._repository.discard_corrupt_by_history(
            task_id=task_id, history_id=history_id
        )

    def discard_conflict(
        self, *, task_id: Optional[str], lease_token: Optional[str], error: object,
    ) -> bool:
        """
        只清理有租约且无法恢复的规划冲突，普通执行错误仍保留重试证据。
        """
        if not isinstance(error, TransferExecutionConflictError) or not task_id or not lease_token:
            return False
        message = str(error)
        if not any(marker in message for marker in (
            "记录已失效", "记录不完整", "版本不一致", "检查点", "恢复状态不完整",
        )):
            return False
        return self.discard_corrupt_task(task_id=task_id, lease_token=lease_token, error=message)

    def discard_failed(
            self,
            *,
            task_id: str,
            history_id: int,
            settlement_revision: int,
    ) -> TransferFailureDiscardResult:
        """
        放弃确定失败任务，使对应历史恢复为普通可维护记录。
        """
        if not task_id or history_id <= 0 or settlement_revision <= 0:
            raise ValueError("放弃失败整理任务缺少任务、历史或结算版本")
        return self._repository.discard_failed(
            task_id=task_id,
            history_id=history_id,
            settlement_revision=settlement_revision,
        )
