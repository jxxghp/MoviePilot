"""整理恢复用例的清理边界与错误证据保留测试。"""

from unittest.mock import Mock

import pytest

from app.application.transfer.execution import TransferExecutionConflictError
from app.application.transfer.recovery import TransferRecoveryCommand
from app.chain.transfer.settlement import _discard_corrupt_transfer_task


@pytest.mark.parametrize("message", ["记录已失效", "记录不完整", "版本不一致", "检查点", "恢复状态不完整"])
def test_corrupt_conflict_discards_owned_task(message):
    """不可恢复的规划冲突必须带当前租约原子清理持久任务。"""
    repository = Mock()
    repository.discard_corrupt_task.return_value = True
    result = TransferRecoveryCommand(repository).discard_conflict(
        task_id="task", lease_token="lease", error=TransferExecutionConflictError(message),
    )
    assert result is True
    repository.discard_corrupt_task.assert_called_once_with(
        task_id="task", lease_token="lease", error=message,
    )


@pytest.mark.parametrize("error,task_id,lease_token", [
    (ValueError("记录不完整"), "task", "lease"),
    (TransferExecutionConflictError("暂时失败"), "task", "lease"),
    (TransferExecutionConflictError("记录不完整"), None, "lease"),
    (TransferExecutionConflictError("记录不完整"), "task", None),
])
def test_recoverable_or_unowned_failure_preserves_evidence(error, task_id, lease_token):
    """普通错误、可重试冲突和缺少租约的任务都不得删除恢复证据。"""
    repository = Mock()
    assert TransferRecoveryCommand(repository).discard_conflict(
        task_id=task_id, lease_token=lease_token, error=error,
    ) is False
    repository.discard_corrupt_task.assert_not_called()


@pytest.mark.parametrize("preview", [True, False])
def test_cleanup_preserves_preview_and_tolerates_repository_failure(preview):
    """预览不清理任务；持久清理失败不阻止内存作业收口。"""
    repository = Mock()
    repository.discard_corrupt_task.side_effect = RuntimeError("存储失败")
    task = Mock(preview=preview, admission_task_id="task", lease_token="lease")
    _discard_corrupt_transfer_task(repository, task, TransferExecutionConflictError("记录不完整"))
    assert repository.discard_corrupt_task.call_count == (0 if preview else 1)


@pytest.mark.parametrize("method,kwargs", [
    ("discard_corrupt_task", {"task_id": "", "lease_token": "lease", "error": "损坏"}),
    ("discard_corrupt_task", {"task_id": "task", "lease_token": "", "error": "损坏"}),
    ("discard_corrupt_task", {"task_id": "task", "lease_token": "lease", "error": ""}),
    ("discard_corrupt_by_history", {"task_id": "task", "history_id": 0}),
    ("discard_failed", {"task_id": "task", "history_id": 7, "settlement_revision": 0}),
])
def test_invalid_cleanup_identity_never_reaches_repository(method, kwargs):
    """缺少身份或结算证据时，必须在写持久层之前拒绝清理。"""
    repository = Mock()
    with pytest.raises(ValueError):
        getattr(TransferRecoveryCommand(repository), method)(**kwargs)
    assert repository.mock_calls == []
