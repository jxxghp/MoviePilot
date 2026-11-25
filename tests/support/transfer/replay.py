"""
待整理任务回放与接纳快照构造；不注册测试用例。
"""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from app.application.transfer.execution import TransferExecutionSnapshot, TransferExecutionState
from app.application.transfer.workflow import TransferAdmission, TransferPlanningInput, TransferTask
from app.chain.transfer.facade import TransferChain
from app.schemas.file import FileItem


def _build_chain(admissions) -> TransferChain:
    """
    构造绕过单例初始化的 TransferChain 骨架。
    :param admissions: durable admission 仓储替身
    :return: TransferChain 骨架
    """
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = admissions
    chain._transfer_executions = MagicMock()
    chain._transfer_executions.get_snapshot.side_effect = (
        lambda *, task_id: _execution_snapshot(task_id=task_id)
    )
    chain.runtime_config = SimpleNamespace(transfer_threads=1)
    chain._worker_owner_id = "test-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._worker_lifecycle_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._replay_stop_event = threading.Event()
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    return chain


def _execution_snapshot(
        *,
        task_id: str = "task-1",
        state: TransferExecutionState = TransferExecutionState.NOT_STARTED,
        steps: tuple[object, ...] = (),
) -> TransferExecutionSnapshot:
    """
    构造回放判定所需的最小执行状态投影。
    """
    return TransferExecutionSnapshot(
        task_id=task_id,
        state=state,
        checkpoint=None,
        retry_generation=0,
        retry_count=0,
        retry_due_at=None,
        settlement_revision=0,
        terminal_history_id=None,
        last_error=None,
        steps=steps,
    )


def _admission(path: str, task_id: str = "task-1") -> TransferAdmission:
    """
    构造一条可脱离数据库会话使用的准入快照。
    """
    planning_input = TransferPlanningInput(
        source_fileitem=_task(path).fileitem.model_dump(mode="json"),
        meta=None,
        mediainfo=None,
    )
    return TransferAdmission(
        task_id=task_id,
        storage="local",
        src_path=path,
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=planning_input,
        lease_owner="test-owner",
        lease_token=f"lease-{task_id}",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )


def _task(path: str, storage: str = "local") -> TransferTask:
    """
    构造测试用整理任务。
    :param path: 源文件路径
    :param storage: 存储
    :return: 整理任务
    """
    file_path = Path(path)
    return TransferTask(fileitem=FileItem(
        storage=storage,
        path=path,
        type="file",
        name=file_path.name,
        basename=file_path.stem,
        extension=file_path.suffix[1:],
    ))
