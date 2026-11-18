"""
远端移动成功但改名中断时，仅恢复同一文件的剩余改名步骤。
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferOperationObservationState,
    TransferStepIntent,
    TransferStepState,
)
from app.application.transfer.workflow import TransferPlanCheckpoint, TransferPlanItem, TransferPlanningInput
from app.chain.transfer import execution as execution_module
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.types import MediaType


class ProcessStopped(BaseException):
    """
    在副作用完成但回执未提交时模拟进程退出。
    """


@pytest.fixture
def remote_move(tmp_path, monkeypatch):
    """
    用真实文件、独立账本和模拟存储回放 CloudDrive 的先移动后改名协议。
    """
    source = tmp_path / "downloads" / "release.S01E01.mkv"
    target = tmp_path / "library" / "Show.S01E01.mkv"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"episode-content")
    local = object.__new__(LocalStorage)
    storage = Mock(spec=StorageBase)
    state = SimpleNamespace(fail_rename=True, crash=None, moves=0, renames=0, lease_token="lease")

    def query(path):
        """
        使用 inode 提供移动、改名后仍保持稳定的远端文件 ID。
        """
        item = local.get_item_strict(Path(path))
        return item.model_copy(update={"storage": "CloudDrive储存"}) if item else None

    def rename(item, name):
        """
        改名故障可持续到下一次调度，也可在改名生效后模拟进程退出。
        """
        state.renames += 1
        if state.fail_rename:
            return False
        Path(item.path).rename(Path(item.path).with_name(name))
        if state.crash == "rename":
            state.crash = None
            raise ProcessStopped()
        return True

    def move(item, directory, name):
        """
        先按源名移动，再调用改名；复现改名失败后源与最终目标均缺失。
        """
        state.moves += 1
        raw = Path(directory) / Path(item.path).name
        Path(item.path).rename(raw)
        if state.crash == "move":
            state.crash = None
            raise ProcessStopped()
        return rename(query(raw), name)

    storage.get_item.side_effect = query
    storage.get_item_strict.side_effect = query
    storage.get_folder.side_effect = query
    storage.is_support_transtype.return_value = True
    storage.move.side_effect = move
    storage.rename.side_effect = rename
    source_item = query(source).model_dump(mode="json")
    planning_input = TransferPlanningInput(source_fileitem=source_item)
    checkpoint = TransferPlanCheckpoint(
        planning_input=planning_input, target_storage="CloudDrive储存",
        root_target_path=target.parent.as_posix(), final_target_path=target.as_posix(),
        resolved_transfer_type="move", overwrite_mode="never",
        items=(TransferPlanItem(
            sequence=0, source_fileitem=source_item,
            target_storage="CloudDrive储存", target_path=target.as_posix(),
        ),),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        TransferPending.__table__, TransferHistory.__table__, TransferExecutionStep.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(TransferPending(
            task_id="remote-move", storage="CloudDrive储存", src_path=source.as_posix(),
            created_at="2026-08-27 09:00:00", updated_at="2026-08-27 09:00:00", state="planned",
            input_version=1, planning_input=planning_input.to_payload(),
            input_fingerprint=planning_input.fingerprint,
            checkpoint_version=checkpoint.schema_version, checkpoint_payload=checkpoint.to_payload(),
            planned_at="2026-08-27 09:00:00", lease_owner="worker", lease_token="lease",
            lease_expires_at="2099-01-01 00:00:00.000000", heartbeat_at="2026-08-27 01:00:00.000000",
            attempt_count=1, execution_state="not_started", retry_generation=0,
            retry_count=0, settlement_revision=0,
        ))
        session.commit()
    repository = TransactionalTransferExecutionRepository(
        factory, local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
        lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
    )
    admissions = TransactionalTransferAdmissionRepository(factory)
    monkeypatch.setattr(admissions, "_lease_now", lambda: datetime(2026, 8, 27, 1, 30, tzinfo=timezone.utc))
    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(execution_module._DurableTransferStepRunner, "_retry_due_at",
                        staticmethod(lambda: "2026-08-27 01:29:00.000000"))

    def execute(*, retry=False):
        """
        重建 runner，并在调度重试时通过真实 admission 重新取得租约。
        """
        if retry:
            claimed = admissions.claim_task(task_id="remote-move", owner_id="retry", lease_seconds=120)
            assert claimed is not None
            state.lease_token = claimed.lease_token
        runner = execution_module._DurableTransferStepRunner(
            task_id="remote-move", lease_token=state.lease_token,
            checkpoint_fingerprint=checkpoint.fingerprint, repository=repository,
        )
        return handler.execute_transfer_plan(
            checkpoint, meta=MetaBase(source.name),
            mediainfo=MediaInfo(type=MediaType.TV, title="Show"),
            source_oper=storage, target_oper=storage, step_runner=runner,
        )

    yield SimpleNamespace(
        source=source, target=target, raw=target.with_name(source.name), storage=storage,
        state=state, execute=execute, repository=repository, checkpoint=checkpoint, query=query,
        admissions=admissions,
    )
    engine.dispose()


def test_remote_move_retries_only_rename_after_partial_success(remote_move):
    """
    改名失败必须等待重试；重建执行器后只补改名，并以最终文件名返回成功。
    """
    case = remote_move
    with pytest.raises(execution_module._TransferRetryDeferred):
        case.execute()
    snapshot = case.repository.get_snapshot(task_id="remote-move")
    assert snapshot.state is TransferExecutionState.RETRY_WAIT
    materialize = next(step for step in snapshot.steps if step.kind == "materialize_target")
    rename = snapshot.steps[-1]
    assert materialize.state is TransferStepState.SUCCEEDED
    assert materialize.result.payload["item"]["path"] == case.raw.as_posix()
    assert rename.kind == "rename_move_target"
    assert rename.state is TransferStepState.FAILED
    assert case.raw.read_bytes() == b"episode-content"
    assert not case.source.exists() and not case.target.exists()
    case.state.fail_rename = False
    result = case.execute(retry=True)
    assert result.success
    assert result.target_item.path == case.target.as_posix()
    assert case.target.read_bytes() == b"episode-content"
    assert not case.raw.exists() and not case.source.exists()
    assert case.state.moves == 1


@pytest.mark.parametrize("crash", ["move", "rename"])
def test_remote_move_recovers_started_step_without_repeating_move(remote_move, crash):
    """
    移动或改名后的进程中断须通过严格观察恢复，已完成移动不会再次发起。
    """
    case = remote_move
    case.state.crash = crash
    case.state.fail_rename = crash == "rename"
    if crash == "rename":
        with pytest.raises(execution_module._TransferRetryDeferred):
            case.execute()
        case.state.fail_rename = False
    with pytest.raises(ProcessStopped):
        case.execute(retry=crash == "rename")
    result = case.execute()
    assert result.success
    assert result.target_item.path == case.target.as_posix()
    assert case.target.read_bytes() == b"episode-content"
    assert not case.raw.exists() and not case.source.exists()
    assert case.state.moves == 1


@pytest.mark.parametrize("crash", [False, True])
def test_remote_move_reuses_legacy_final_receipt(remote_move, crash):
    """
    原有一次性移动改名成功或最终回执丢失时，继续复用原步骤且不追加改名。
    """
    case = remote_move
    case.state.fail_rename = False
    if crash:
        case.state.crash = "rename"
        with pytest.raises(ProcessStopped):
            case.execute()
    assert case.execute().success
    case.storage.move.side_effect = AssertionError("不能重放已完成移动")
    case.storage.rename.side_effect = AssertionError("不能重复改名")
    assert case.execute().success
    assert all(step.kind != "rename_move_target"
               for step in case.repository.get_snapshot(task_id="remote-move").steps)
    assert case.state.moves == 1
    assert case.state.renames == 1


@pytest.mark.parametrize("collision", ["raw", "final"])
def test_remote_move_retry_preserves_concurrent_files(remote_move, collision):
    """
    重试改名前若原名落点被替换或最终路径被占用，保留现场并转人工复核。
    """
    case = remote_move
    with pytest.raises(execution_module._TransferRetryDeferred):
        case.execute()
    case.state.fail_rename = False
    path = case.raw if collision == "raw" else case.target
    if collision == "raw":
        case.raw.rename(case.raw.with_suffix(".retained"))
    path.write_bytes(b"unrelated-file!")
    before = case.state.renames
    with pytest.raises(execution_module._TransferManualReviewRequired):
        case.execute(retry=True)
    assert path.read_bytes() == b"unrelated-file!"
    assert case.state.moves == 1
    assert case.state.renames == before
    assert case.repository.get_snapshot(task_id="remote-move").state is TransferExecutionState.MANUAL_REVIEW


@pytest.mark.parametrize("change", ["id", "size", "type", "missing_id", "missing_size", "query"])
def test_remote_move_keeps_unverified_intermediate_for_review(remote_move, change):
    """
    原名落点身份不符、凭据不足或查询失败时，不能自动改名或重放移动。
    """
    case = remote_move
    case.state.crash = "move"
    with pytest.raises(ProcessStopped):
        case.execute()

    def query(path):
        """
        仅改变原名落点的身份或可查询性，保留实际文件供断言。
        """
        item = case.query(path)
        if Path(path) == case.raw:
            if change == "query":
                raise StorageQueryError("CloudDrive unavailable")
            field, value = {"id": ("fileid", "other"), "size": ("size", 1), "type": ("type", "dir"),
                            "missing_id": ("fileid", None), "missing_size": ("size", None)}[change]
            return item.model_copy(update={field: value})
        return item

    case.storage.get_item_strict.side_effect = query
    with pytest.raises(execution_module._TransferManualReviewRequired):
        case.execute()
    assert case.repository.get_snapshot(task_id="remote-move").state is TransferExecutionState.MANUAL_REVIEW
    assert case.raw.read_bytes() == b"episode-content"
    assert not case.target.exists()
    assert case.state.moves == 1
    assert case.state.renames == 0


@pytest.mark.parametrize("missing", ["frozen_id", "intermediate"])
def test_remote_move_observation_requires_stable_source_and_visible_landing(remote_move, missing):
    """
    原名落点缺失或冻结源无稳定 ID 时，即使文件同名同大小也不能确认移动成功。
    """
    case = remote_move
    case.state.crash = "move"
    with pytest.raises(ProcessStopped):
        case.execute()
    source = FileItem.model_validate(case.checkpoint.items[0].source_fileitem)
    if missing == "frozen_id":
        source.fileid = None
    else:
        case.raw.unlink()
    observation = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source, target_storage="CloudDrive储存", source_oper=case.storage,
        target_oper=case.storage, target_file=case.target, transfer_type="move",
    )
    assert observation.state is (
        TransferOperationObservationState.UNKNOWN if missing == "frozen_id"
        else TransferOperationObservationState.CONFLICT
    )
    assert case.state.moves == 1 and case.state.renames == 0


@pytest.mark.parametrize("tamper", ["source", "target", "storage", "predecessor"])
def test_remote_move_rename_intent_is_bound_to_plan_and_receipt(remote_move, tamper):
    """
    补改名只能消费本计划原名落点的成功移动回执，不能指定其它路径或绕过移动。
    """
    case = remote_move
    if tamper != "predecessor":
        with pytest.raises(execution_module._TransferRetryDeferred):
            case.execute()
    snapshot = case.repository.get_snapshot(task_id="remote-move")
    payload = {"storage": "CloudDrive储存", "source_path": case.raw.as_posix(),
               "target_path": case.target.as_posix()}
    if tamper != "predecessor":
        key = {"source": "source_path", "target": "target_path", "storage": "storage"}[tamper]
        payload[key] = "/unrelated.mkv"
    intent = TransferStepIntent.create(
        task_id="remote-move", checkpoint_fingerprint=case.checkpoint.fingerprint,
        ordinal=len(snapshot.steps), phase="transfer", kind="rename_move_target", payload=payload,
    )
    token = "lease"
    if tamper != "predecessor":
        token = case.admissions.claim_task(
            task_id="remote-move", owner_id="retry", lease_seconds=120,
        ).lease_token
    with pytest.raises(TransferExecutionConflictError):
        case.repository.prepare_step(
            task_id="remote-move", lease_token=token, intent=intent,
        )
