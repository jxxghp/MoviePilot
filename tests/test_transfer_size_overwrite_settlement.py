"""贯通大小覆盖裁决与 durable 结算，防止重复整理降级成功历史。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.application.history.retry import HistoryGateAction, evaluate_history_gate
from app.application.transfer.execution import TransferSettlementResult
from app.chain.transfer.execution import _DurableTransferStepRunner
from app.modules.filemanager.transhandler import TransHandler
from tests.test_transfer_job_manager import (
    FakeMedia,
    bind_terminal_checkpoint,
    make_fileitem,
    make_task,
    make_transfer_chain,
)


def _resolve_size_overwrite(task, target_size):
    """调用真实大小覆盖分支，仅替换目标存储查询与插件事件边界。"""
    task.mediainfo = FakeMedia()
    target = make_fileitem("/library/Test.Show.S01E01.mkv", size=target_size)
    storage = MagicMock()
    storage.get_item_strict.return_value = target
    result = TransHandler()._TransHandler__resolve_overwrite(
        fileitem=task.fileitem,
        meta=task.meta,
        mediainfo=task.mediainfo,
        target_oper=storage,
        target_storage="local",
        target_file=Path(target.path),
        transfer_type="link",
        overwrite_mode="size",
        need_notify=True,
    )
    storage.get_item_strict.assert_called_once_with(Path(target.path))
    return result


@pytest.fixture
def overwrite_event(monkeypatch):
    """隔离插件覆盖事件，保留宿主大小策略的真实判断。"""
    event = MagicMock(return_value=None)
    monkeypatch.setattr(
        "app.modules.filemanager.transhandler.eventmanager.send_event", event
    )
    return event


@pytest.mark.parametrize("target_size", [512, 1024, 2048])
def test_video_size_overwrite_marks_policy_decline(target_size, overwrite_event):
    """仅更大的源文件允许升级，同大小与更小文件应携带明确覆盖跳过事实。"""
    task = make_task(1)
    over_flag, latest, result = _resolve_size_overwrite(task, target_size)

    assert latest is False
    overwrite_event.assert_called_once()
    if target_size < task.fileitem.size:
        assert over_flag is True
        assert result is None
    else:
        assert over_flag is False
        assert result.success is False
        assert result.overwrite_skipped is True
        assert result.target_item.size == target_size
        assert result.message == "媒体库存在同名文件，且质量更好"


@pytest.mark.parametrize("target_size", [1024, 2048])
def test_repeated_size_decline_preserves_successful_settlement(
    target_size, overwrite_event, monkeypatch,
):
    """mtime 变化重送后的真实大小裁决必须保留成功历史并静默结算。"""
    task = make_task(1)
    task.fileitem.modify_time = 150.0
    history = SimpleNamespace(
        id=99,
        status=True,
        src_fileitem={"size": task.fileitem.size, "modify_time": 100.0},
    )
    assert evaluate_history_gate(
        history,
        file_size=task.fileitem.size,
        file_modify_time=task.fileitem.modify_time,
    ) == HistoryGateAction.PASS_SIZE_CHANGED
    _, _, result = _resolve_size_overwrite(task, target_size)
    overwrite_event.assert_called_once()

    runner = object.__new__(_DurableTransferStepRunner)
    runner._task_id = "size-overwrite-task"
    runner._lease_token = "lease"
    runner._operation_ids = []
    runner._command = MagicMock()
    runner._command.checkpoint.side_effect = lambda **kwargs: SimpleNamespace(
        checkpoint=kwargs["checkpoint"]
    )
    checkpoint = runner.checkpoint(result)
    assert checkpoint.payload["outcome"] == "overwrite_skipped"
    bind_terminal_checkpoint(task, result)
    task.bind_execution_checkpoint(checkpoint)

    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.post_message = MagicMock()
    chain.queue_failed_transfer_notification = MagicMock()
    chain.transfer_history_repository.get_by_src.return_value = history
    assert chain._TransferChain__put_to_jobview(task)
    staging = MagicMock()
    staging.get_success_by_src.return_value = history

    def settle(**kwargs):
        """核实 writer 回读旧历史，并以无通知的成功终态结算。"""
        assert kwargs["topic"] is None
        assert kwargs["publish"] is None
        assert kwargs["settlement"].outcome == "succeeded"
        assert kwargs["settlement"].error is None
        assert kwargs["stage_history"](staging) is history
        return TransferSettlementResult(
            history_id=history.id, settlement_revision=1, pending_deleted=True,
        )

    chain.durable_event_writer.transfer_result.side_effect = settle
    add_fail = MagicMock()
    record_failure = MagicMock()
    monkeypatch.setattr("app.chain.transfer.settlement.add_transfer_fail", add_fail)
    monkeypatch.setattr(
        "app.chain.transfer.settlement.record_transfer_failure", record_failure
    )
    state, message = chain._TransferChain__default_callback(task, result)

    assert state is False
    assert message == result.message
    assert task.terminal_settled is True
    assert history.status is True
    staging.get_success_by_src.assert_called_once_with(
        task.fileitem.path, task.fileitem.storage,
    )
    chain.durable_event_writer.transfer_result.assert_called_once()
    add_fail.assert_not_called()
    record_failure.assert_not_called()
    chain.eventmanager.send_event.assert_not_called()
    chain.post_message.assert_not_called()
    chain.queue_failed_transfer_notification.assert_not_called()


@pytest.mark.parametrize("history_status", [None, False])
def test_size_decline_without_success_history_keeps_failure(
    history_status, overwrite_event,
):
    """目标来自其他源或只有失败历史时，大小拒绝仍按失败处理。"""
    task = make_task(1)
    _, _, result = _resolve_size_overwrite(task, task.fileitem.size)
    overwrite_event.assert_called_once()
    chain = make_transfer_chain()
    chain.transfer_history_repository.get_by_src.return_value = (
        None if history_status is None else SimpleNamespace(id=99, status=False)
    )
    chain.transfer_history_repository.get_success_by_src.return_value = None
    bind_terminal_checkpoint(task, result)
    declined = chain._is_overwrite_declined(
        task, result, chain.transfer_history_repository,
    )
    settlement = chain._TransferChain__build_transfer_result_settlement(
        task, result, overwrite_declined=declined,
    )

    assert declined is False
    assert settlement.outcome == "failed"
    assert settlement.error == result.message
