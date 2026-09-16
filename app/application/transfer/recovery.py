"""失败与损坏整理任务的证据清理和历史解绑用例。"""

from typing import Optional

from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionRepository,
    TransferFailureDiscardResult,
)


class TransferRecoveryCommand:
    """把损坏任务恢复交给持久层原子清理，保留历史供重新规划。"""

    def __init__(self, repository: TransferExecutionRepository) -> None:
        """保存拥有租约校验与事务的执行仓储。"""
        self._repository = repository

    def discard_corrupt_task(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: str,
    ) -> bool:
        """以当前租约清理损坏任务，避免恢复线程再次回放旧步骤。"""
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
        """放弃无法重试的损坏任务，保留历史记录供重新生成计划。"""
        if not task_id or history_id <= 0:
            raise ValueError("放弃损坏任务缺少任务或历史")
        return self._repository.discard_corrupt_by_history(
            task_id=task_id, history_id=history_id
        )

    def discard_conflict(
        self, *, task_id: Optional[str], lease_token: Optional[str], error: object,
    ) -> bool:
        """只清理有租约且无法恢复的规划冲突，普通执行错误仍保留重试证据。"""
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
        """放弃确定失败任务，使对应历史恢复为普通可维护记录。"""
        if not task_id or history_id <= 0 or settlement_revision <= 0:
            raise ValueError("放弃失败整理任务缺少任务、历史或结算版本")
        return self._repository.discard_failed(
            task_id=task_id,
            history_id=history_id,
            settlement_revision=settlement_revision,
        )
