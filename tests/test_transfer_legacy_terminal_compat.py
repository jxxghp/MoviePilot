"""验证旧插件同步整理 ABI 复用 canonical durable 终态写入口。"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from app.application.history import TransferHistorySnapshot
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferSettlementResult,
)
from app.chain.transfer.facade import TransferChain
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType


def _fileitem(*, fileid: str = "source-v1", size: int = 1024) -> FileItem:
    """构造可区分同路径版本的兼容调用文件项。"""
    return FileItem(
        storage="local",
        path="/downloads/Movie.2026.mkv",
        type="file",
        name="Movie.2026.mkv",
        basename="Movie.2026",
        extension="mkv",
        size=size,
        modify_time=1770000000 + size,
        fileid=fileid,
    )


def _result(task, *, success: bool, overwrite_skipped: bool = False) -> TransferInfo:
    """按当前任务构造足以写历史的兼容整理结果。"""
    return TransferInfo(
        success=success,
        overwrite_skipped=overwrite_skipped,
        message=None if success else "copy failed",
        fileitem=task.fileitem,
        target_item=FileItem(
            storage="local",
            path="/library/Movie (2026)/Movie.mkv",
            type="file",
            name="Movie.mkv",
            basename="Movie",
            extension="mkv",
        ),
        transfer_type="copy",
        file_list=[task.fileitem.path],
    )


def _compat_chain(result_factory):
    """构造只执行旧同步命令和 task-aware 结算的 TransferChain 骨架。"""
    chain = object.__new__(TransferChain)
    chain.transfer_history_repository = SimpleNamespace(
        get_by_src=lambda _src, storage=None: None
    )
    chain.transfer_execution_repository = Mock()
    chain._worker_owner_id = "compat-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain.durable_event_writer = Mock()
    executed = []

    def execute(task, **_kwargs):
        """模拟外部步骤已完成并建立可独立结算的执行检查点。"""
        executed.append(task.fileitem.fileid)
        task_id = f"task-{task.fileitem.fileid}"
        lease_token = f"lease-{task.fileitem.fileid}"
        task.bind_admission_task_id(task_id)
        task.bind_execution_lease(
            owner_id=chain._worker_owner_id,
            lease_token=lease_token,
        )
        chain._owned_leases[task_id] = (lease_token, float("inf"))
        result = result_factory(task)
        task.bind_execution_checkpoint(TransferExecutionCheckpoint.create(
            payload={
                "outcome": (
                    "overwrite_skipped"
                    if result.overwrite_skipped
                    else "succeeded" if result.success else "failed"
                ),
                "transferinfo": result.model_dump(mode="json"),
            },
            operation_ids=(f"operation-{task.fileitem.fileid}",),
        ))
        return result

    chain._plan_checkpoint_and_execute = Mock(side_effect=execute)
    return chain, executed


def _invoke(chain: TransferChain, fileitem: FileItem) -> TransferInfo:
    """以插件可见参数调用同步兼容入口。"""
    return chain.execute_legacy_transfer_command(
        fileitem=fileitem,
        meta=MetaBase(fileitem.name),
        mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026"),
        target_storage="local",
        target_path="/library",
        transfer_type="copy",
    )


def _settlement_writer(*, status: bool, history_id: int = 41):
    """返回执行 stage_history 并产出 task-aware 结果的 writer side effect。"""
    staged_payloads = []

    def write(**kwargs):
        """暂存历史后返回与当前结算修订一致的投影。"""
        staging = Mock()

        def replace_history(history):
            """保存历史 payload 并返回 writer 所需的最小记录。"""
            payload = history.to_payload()
            staged_payloads.append(payload)
            return TransferHistorySnapshot(
                id=history_id,
                status=history.status,
                src=history.src,
                src_storage=history.src_storage,
                src_fileitem=history.src_fileitem,
            )

        staging.replace.side_effect = replace_history
        staging.get_success_by_src.return_value = TransferHistorySnapshot(
            id=history_id,
            status=True,
            src="/downloads/Movie.2026.mkv",
            src_storage="local",
            src_fileitem=_fileitem().model_dump(mode="json"),
        )
        history = kwargs["stage_history"](staging)
        assert bool(history.status) is status
        return TransferSettlementResult(
            history_id=history.id,
            settlement_revision=1,
            pending_deleted=status,
        )

    return write, staged_payloads


def test_legacy_failed_result_uses_atomic_terminal_writer() -> None:
    """失败结果也必须原子写失败历史并保留 pending，不得直接注销。"""
    chain, executed = _compat_chain(lambda task: _result(task, success=False))
    write, staged_payloads = _settlement_writer(status=False)
    chain.durable_event_writer.transfer_result.side_effect = write

    returned = _invoke(chain, _fileitem())

    assert returned.success is False
    assert returned.message == "copy failed"
    assert executed == ["source-v1"]
    assert staged_payloads[0]["status"] == 0
    call = chain.durable_event_writer.transfer_result.call_args.kwargs
    assert call["topic"] is None
    assert call["publish"] is None
    assert call["settlement"].outcome == "failed"
    assert call["settlement"].error == "copy failed"


def test_legacy_settlement_response_loss_replays_receipt_by_same_task_id() -> None:
    """首次提交后响应丢失只能用同一 task_id 回读，不得重做外部步骤。"""
    chain, executed = _compat_chain(lambda task: _result(task, success=True))
    write, staged_payloads = _settlement_writer(status=True)
    writer_calls = 0

    def response_lost_then_receipt(**kwargs):
        """首次提交历史后模拟响应丢失，第二次只返回 immutable receipt。"""
        nonlocal writer_calls
        writer_calls += 1
        if writer_calls == 1:
            write(**kwargs)
            raise RuntimeError("response lost after commit")
        return TransferSettlementResult(
            history_id=41,
            settlement_revision=1,
            pending_deleted=True,
            already_settled=True,
        )

    chain.durable_event_writer.transfer_result.side_effect = response_lost_then_receipt

    returned = _invoke(chain, _fileitem())

    assert returned.success is True
    assert executed == ["source-v1"]
    assert writer_calls == 2
    assert len(staged_payloads) == 1
    first = chain.durable_event_writer.transfer_result.call_args_list[0].kwargs
    second = chain.durable_event_writer.transfer_result.call_args_list[1].kwargs
    assert second["settlement"] == first["settlement"]


def test_legacy_settlement_double_failure_releases_claim_without_deleting_evidence(
) -> None:
    """两次 writer 均失败时必须释放 lease，并保留 pending 与步骤供恢复。"""
    chain, executed = _compat_chain(lambda task: _result(task, success=True))
    chain._transfer_admissions = Mock()
    chain._transfer_admissions.release_claim.return_value = True
    chain._TransferChain__ensure_recovery_scheduler = Mock()
    chain.durable_event_writer.transfer_result.side_effect = RuntimeError(
        "writer unavailable"
    )

    returned = _invoke(chain, _fileitem())

    assert returned.success is False
    assert returned.message == "整理结果确认失败，后台将自动重试"
    assert "writer unavailable" not in (returned.message or "")
    assert executed == ["source-v1"]
    assert chain.durable_event_writer.transfer_result.call_count == 2
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="task-source-v1",
        lease_token="lease-source-v1",
        error=(
            "旧整理兼容命令 durable 终态结算失败：writer unavailable"
        ),
    )
    chain._transfer_admissions.abandon_unstarted.assert_not_called()
    assert chain._owned_leases == {}


def test_legacy_overwrite_skip_binds_existing_success_in_atomic_writer() -> None:
    """覆盖跳过复用既有成功历史并以 succeeded 终态结算。"""
    chain, executed = _compat_chain(
        lambda task: _result(task, success=False, overwrite_skipped=True)
    )
    write, staged_payloads = _settlement_writer(status=True)
    chain.durable_event_writer.transfer_result.side_effect = write

    success_history = SimpleNamespace(id=41, status=True)
    history_port = SimpleNamespace(
        get_by_src=lambda _src, storage=None: success_history,
    )
    chain.transfer_history_repository = history_port
    returned = _invoke(chain, _fileitem())

    assert returned.success is False
    assert returned.overwrite_skipped is True
    assert executed == ["source-v1"]
    assert staged_payloads == []
    settlement = chain.durable_event_writer.transfer_result.call_args.kwargs[
        "settlement"
    ]
    assert settlement.outcome == "succeeded"
    assert settlement.error is None


def test_legacy_overwrite_skip_without_success_history_settles_failed() -> None:
    """兼容入口没有既有成功历史时按失败结算并保留 pending。"""
    chain, executed = _compat_chain(
        lambda task: _result(task, success=False, overwrite_skipped=True)
    )
    write, staged_payloads = _settlement_writer(status=False)
    chain.durable_event_writer.transfer_result.side_effect = write
    history_port = SimpleNamespace(
        get_by_src=lambda _src, storage=None: None,
    )
    chain.transfer_history_repository = history_port
    returned = _invoke(chain, _fileitem())

    assert returned.success is False
    assert returned.overwrite_skipped is True
    assert executed == ["source-v1"]
    assert len(staged_payloads) == 1
    assert staged_payloads[0]["status"] == 0
    settlement = chain.durable_event_writer.transfer_result.call_args.kwargs[
        "settlement"
    ]
    assert settlement.outcome == "failed"
    assert settlement.error == "copy failed"


def test_legacy_same_path_changed_version_still_executes_new_task() -> None:
    """兼容入口不能仅凭同源历史吞掉 fileid/size/mtime 已变化的新版本。"""
    chain, executed = _compat_chain(lambda task: _result(task, success=True))
    write, _staged_payloads = _settlement_writer(status=True)
    chain.durable_event_writer.transfer_result.side_effect = write

    first = _invoke(chain, _fileitem(fileid="source-v1", size=1024))
    second = _invoke(chain, _fileitem(fileid="source-v2", size=2048))

    assert first.success is True
    assert second.success is True
    assert executed == ["source-v1", "source-v2"]
