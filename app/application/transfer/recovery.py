"""
整理任务的只读 AI 接管校验、失败证据恢复和历史解绑用例。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionRepository,
    TransferExecutionSnapshot,
    TransferExecutionState,
    TransferFailureDiscardResult,
    TransferSettlementSnapshot,
    TransferTerminalState,
)
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType

if TYPE_CHECKING:
    from app.application.history import TransferHistoryRepository, TransferHistorySnapshot
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


def ai_takeover_result(
        *, history: TransferHistorySnapshot, repository: TransferExecutionRepository,
        histories: TransferHistoryRepository,
) -> tuple[Literal["queued", "resolved", "attention", "unknown"], str]:
    """
    根据持久任务和最新历史核实接管结果，不把模型分析结束当作整理成功。

    :param history: 分析开始前读取的原始失败历史身份
    :param repository: 读取任务执行状态的端口；成功结算可能已删除 pending
    :param histories: 读取同一历史最新投影的端口
    :return: 通知阶段及宿主生成的简短说明；缺失或冲突证据明确标为未知
    """
    task_id = history.transfer_task_id
    snapshot = repository.get_snapshot(task_id=task_id) if task_id else None
    receipt = repository.get_latest_settlement(task_id=task_id) if task_id and snapshot is None else None
    return _ai_takeover_result(history, histories.get(history.id), snapshot, receipt)


async def async_ai_takeover_result(
        *, history: TransferHistorySnapshot, repository: TransferExecutionRepository,
        histories: TransferHistoryRepository,
) -> tuple[Literal["queued", "resolved", "attention", "unknown"], str]:
    """
    使用既有异步查询端口核实自动接管结果，不额外创建并发拥有者。

    :param history: 分析开始前冻结的失败身份
    :param repository: 持久任务和独立结算回执的异步查询端口
    :param histories: 当前历史的异步查询端口
    :return: 经宿主证据核实的通知阶段和简短说明
    """
    task_id = history.transfer_task_id
    snapshot = await repository.async_get_snapshot(task_id=task_id) if task_id else None
    receipt = await repository.async_get_latest_settlement(task_id=task_id) if task_id and snapshot is None else None
    return _ai_takeover_result(history, await histories.async_get(history.id), snapshot, receipt)


def _ai_takeover_result(
        history: TransferHistorySnapshot, current: Optional[TransferHistorySnapshot],
        snapshot: Optional[TransferExecutionSnapshot], receipt: Optional[TransferSettlementSnapshot],
) -> tuple[Literal["queued", "resolved", "attention", "unknown"], str]:
    """
    统一解释同步、异步查询证据；成功必须有原任务更新的独立终态回执。
    """
    if (receipt is not None and receipt.task_id == history.transfer_task_id
            and receipt.settlement_revision > (history.transfer_settlement_revision or 0)
            and receipt.outcome is TransferTerminalState.SUCCEEDED
            and (receipt.src, receipt.src_storage) == (history.src, history.src_storage)):
        return "resolved", "原整理任务已有更新的独立成功回执，入库结果沿用普通整理通知。"
    if current is None or (current.src, current.src_storage) != (history.src, history.src_storage):
        return "unknown", "原整理历史已不存在或源身份发生变化，请打开整理历史核实。"
    if current.transfer_task_id not in {None, history.transfer_task_id}:
        return "unknown", "资源已关联其他整理任务，本轮分析不能确认其最终结果，请人工核实。"
    if snapshot is not None:
        if snapshot.state in {TransferExecutionState.FAILED, TransferExecutionState.MANUAL_REVIEW}:
            if current.status:
                return "unknown", "任务与整理历史状态不一致，请人工核实，未判定为整理成功。"
            reason = (snapshot.last_error or current.errmsg or "未能自动修正识别或规划").strip()
            return "attention", f"自动接管后仍需人工处理：{reason[:400]}。请打开整理历史核对后重试。"
        return "queued", "任务正在排队或执行，尚未确认入库；后续成败由正常整理结果通知报告。"
    return "unknown", "未找到可核实的原任务或更新的成功回执，请人工查看整理历史；不能确认已入库。"


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
