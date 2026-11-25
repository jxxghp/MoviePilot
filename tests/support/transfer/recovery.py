"""
整理结算恢复的任务、规划与执行回执；不注册测试用例。
"""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionSnapshot,
    TransferExecutionState,
)
from app.application.transfer.workflow import (
    TransferAdmission,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferTask,
)
from app.chain.transfer.facade import TransferChain
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


def _planning_input(path: str) -> TransferPlanningInput:
    """
    构造保留源文件身份的最小持久规划输入。
    """
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": path,
            "type": "file",
            "name": Path(path).name,
            "basename": Path(path).stem,
            "extension": Path(path).suffix.lstrip("."),
            "size": 1024,
        },
        meta=None,
        mediainfo=None,
        requested_transfer_type="move",
    )


def _plan_checkpoint(
        planning_input: TransferPlanningInput,
) -> TransferPlanCheckpoint:
    """
    构造外部步骤已经结束后可直接结算的冻结计划。
    """
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library/Movie.mkv",
        resolved_transfer_type="move",
        items=(),
        need_notify=False,
        skip_reason="测试已完成外部步骤",
    )


def _transfer_result(path: str, *, success: bool) -> TransferInfo:
    """
    构造可完整写入 execution checkpoint 的整理结果。
    """
    fileitem = FileItem(
        storage="local",
        path=path,
        type="file",
        name=Path(path).name,
        basename=Path(path).stem,
        extension=Path(path).suffix.lstrip("."),
        size=1024,
    )
    return TransferInfo(
        success=success,
        fileitem=fileitem,
        target_item=(
            FileItem(
                storage="local",
                path="/library/Movie.mkv",
                type="file",
                name="Movie.mkv",
            )
            if success
            else None
        ),
        transfer_type="move",
        fail_list=[] if success else [path],
        message="整理完成" if success else "整理失败",
        need_notify=False,
    )


def _build_chain(admissions) -> TransferChain:
    """
    构造只允许执行 settling 终态恢复的 TransferChain 骨架。
    """
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = admissions
    chain._transfer_executions = MagicMock()
    chain._worker_owner_id = "recovery-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._replay_stop_event = threading.Event()
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    chain._TransferChain__restore_planned_task = MagicMock()
    chain._TransferChain__select_storage_oper = MagicMock(
        side_effect=AssertionError("settling 恢复不得选择存储适配器")
    )
    chain._plan_checkpoint_and_execute = MagicMock(
        side_effect=AssertionError("settling 恢复不得重新执行计划")
    )
    chain.jobview = MagicMock()
    return chain


def _recovered_task(
        chain: TransferChain,
        admission: TransferAdmission,
        execution_checkpoint: TransferExecutionCheckpoint,
) -> TransferTask:
    """
    把 claim 投影绑定为只待终态 writer 处理的恢复任务。
    """
    assert admission.planning_input is not None
    assert admission.checkpoint is not None
    assert admission.lease_owner is not None
    assert admission.lease_token is not None
    task = TransferTask(
        fileitem=FileItem.model_validate(
            admission.planning_input.source_fileitem
        )
    )
    task.bind_admission_task_id(admission.task_id)
    task.bind_planning_input(admission.planning_input)
    task.bind_plan_checkpoint(admission.checkpoint)
    task.bind_execution_checkpoint(execution_checkpoint)
    task.bind_execution_lease(
        owner_id=admission.lease_owner,
        lease_token=admission.lease_token,
    )
    chain._owned_leases[admission.task_id] = (
        admission.lease_token,
        time.monotonic() + 120,
    )
    chain._transfer_executions.get_snapshot.return_value = TransferExecutionSnapshot(
        task_id=admission.task_id,
        state=TransferExecutionState.SETTLING,
        checkpoint=execution_checkpoint,
        retry_generation=0,
        retry_count=0,
        retry_due_at=None,
        settlement_revision=0,
        terminal_history_id=None,
        last_error=None,
        steps=(),
    )
    return task
