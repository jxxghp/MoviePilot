from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.history import (
    DownloadHistoryMutationCommand,
    TransferHistoryMutationCommand,
)
from app.application.transfer.execution import (
    TransferExecutionState,
    TransferFailureDiscardResult,
)


def _transfer_command(*, history=None, delete_result=True, commit_error=None):
    """构造可观察整理历史事务和外部副作用的命令。"""
    repository = Mock()
    repository.get.return_value = history
    download_repository = Mock()
    transfer_execution_repository = Mock()
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = commit_error
    dependencies = {
        "repository": repository,
        "download_repository": download_repository,
        "transfer_execution_repository": transfer_execution_repository,
        "unit_of_work": unit_of_work,
        "file_item_factory": lambda payload: SimpleNamespace(**payload),
        "delete_media_file": Mock(return_value=delete_result),
        "publish_download_file_deleted": Mock(),
        "clear_failures": Mock(),
    }
    return TransferHistoryMutationCommand(**dependencies), dependencies


def _history():
    """构造包含源和目标文件信息的整理历史快照。"""
    return SimpleNamespace(
        id=7,
        src="/downloads/demo.mkv",
        src_storage="local",
        download_hash="abc",
        src_fileitem={"path": "/downloads/demo.mkv"},
        dest_fileitem={"path": "/media/demo.mkv"},
        transfer_task_id=None,
        transfer_settlement_revision=None,
    )


def test_download_history_delete_rolls_back_commit_failure():
    """下载历史提交失败时必须回滚请求级事务。"""
    repository = Mock()
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = RuntimeError("commit failed")
    command = DownloadHistoryMutationCommand(
        repository=repository,
        unit_of_work=unit_of_work,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        command.delete(8)

    repository.stage_delete_history.assert_called_once_with(8)
    unit_of_work.rollback.assert_called_once_with()


def test_transfer_source_delete_failure_keeps_database_unchanged():
    """源文件删除失败时不得删除历史或更新下载文件状态。"""
    command, dependencies = _transfer_command(
        history=_history(),
        delete_result=False,
    )

    result = command.delete(7, delete_source=True)

    assert result.success is False
    assert result.message == "/downloads/demo.mkv 删除失败"
    assert result.source.status == "failed"
    assert result.destination.status == "not_requested"
    dependencies["repository"].stage_delete.assert_not_called()
    dependencies["download_repository"].stage_delete_file_by_fullpath.assert_not_called()
    dependencies["unit_of_work"].commit.assert_not_called()


def test_transfer_delete_commits_before_event_and_retry_cleanup():
    """整理记录提交成功后才发送文件删除事件并清理失败计数。"""
    calls = []
    command, dependencies = _transfer_command(history=_history())
    dependencies["unit_of_work"].commit.side_effect = lambda: calls.append("commit")
    dependencies["publish_download_file_deleted"].side_effect = (
        lambda _payload: calls.append("event")
    )
    dependencies["clear_failures"].side_effect = lambda *_args: calls.append("clear")

    result = command.delete(
        7,
        delete_source=True,
        delete_destination=True,
    )

    assert result.success is True
    assert result.source.status == "deleted"
    assert result.destination.status == "deleted"
    dependencies["download_repository"].stage_delete_file_by_fullpath.assert_called_once_with(
        "/downloads/demo.mkv"
    )
    dependencies["repository"].stage_delete.assert_called_once_with(7)
    assert calls == ["commit", "event", "clear"]


def test_transfer_commit_failure_suppresses_event_and_retry_cleanup():
    """数据库提交失败时不得发布已删除事件或清除重试状态。"""
    command, dependencies = _transfer_command(
        history=_history(),
        commit_error=RuntimeError("commit failed"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        command.delete(7, delete_source=True)

    dependencies["unit_of_work"].rollback.assert_called_once_with()
    dependencies["publish_download_file_deleted"].assert_not_called()
    dependencies["clear_failures"].assert_not_called()


def test_transfer_truncate_uses_single_transaction():
    """清空整理历史只暂存一次并统一提交。"""
    command, dependencies = _transfer_command()

    result = command.truncate()

    assert result.success is True
    assert result.message == "已清空旧整理记录，失败任务记录已保留"
    dependencies["repository"].stage_truncate.assert_called_once_with()
    dependencies["unit_of_work"].commit.assert_called_once_with()


def test_transfer_delete_rejects_nonfailed_durable_receipt_before_file_side_effects():
    """非 FAILED durable 回执不能被历史 API 连同文件一起删除。"""
    history = _history()
    history.transfer_task_id = "task-durable"
    history.transfer_settlement_revision = 2
    command, dependencies = _transfer_command(history=history)
    dependencies["transfer_execution_repository"].discard_failed.return_value = (
        TransferFailureDiscardResult(
            discarded=False,
            state=TransferExecutionState.MANUAL_REVIEW,
            message="这条整理任务需要先完成人工确认，再重试",
        )
    )

    result = command.delete(7, delete_source=True, delete_destination=True)

    assert result.success is False
    assert result.message == "这条整理任务需要先完成人工确认，再重试"
    assert result.history == "retained"
    dependencies["delete_media_file"].assert_not_called()
    dependencies["repository"].stage_delete.assert_not_called()
    dependencies["unit_of_work"].commit.assert_not_called()


def test_transfer_delete_discards_failed_durable_receipt_before_cleanup():
    """FAILED durable 回执应先放弃任务，再按普通历史执行文件和记录删除。"""
    history = _history()
    history.transfer_task_id = "task-durable"
    history.transfer_settlement_revision = 3
    command, dependencies = _transfer_command(history=history)
    dependencies["transfer_execution_repository"].discard_failed.return_value = (
        TransferFailureDiscardResult(
            discarded=True,
            state=TransferExecutionState.FAILED,
            message="已放弃这条失败的整理任务",
        )
    )

    result = command.delete(7, delete_destination=True)

    assert result.success is True
    dependencies["transfer_execution_repository"].discard_failed.assert_called_once_with(
        task_id="task-durable",
        history_id=7,
        settlement_revision=3,
    )
    dependencies["delete_media_file"].assert_called_once()
    dependencies["repository"].stage_delete.assert_called_once_with(7)
    dependencies["unit_of_work"].commit.assert_called_once_with()


def test_transfer_destination_delete_failure_keeps_history_and_reports_step():
    """目标文件删除失败时必须保留历史，并向调用方报告失败步骤。"""
    command, dependencies = _transfer_command(
        history=_history(),
        delete_result=False,
    )

    result = command.delete(7, delete_destination=True)

    assert result.success is False
    assert result.destination.status == "failed"
    assert result.history == "retained"
    assert result.source.status == "not_requested"
    dependencies["repository"].stage_delete.assert_not_called()
    dependencies["unit_of_work"].commit.assert_not_called()


def test_transfer_delete_commits_completed_source_when_destination_fails():
    """一项文件已完成而另一项失败时，已完成源文件状态仍应提交并保留历史。"""
    repository = Mock()
    repository.get.return_value = _history()
    dependencies = {
        "repository": repository,
        "download_repository": Mock(),
        "transfer_execution_repository": Mock(),
        "unit_of_work": Mock(),
        "file_item_factory": lambda payload: SimpleNamespace(**payload),
        "file_exists": Mock(return_value=True),
        "delete_media_file": Mock(side_effect=[False, True]),
        "publish_download_file_deleted": Mock(),
        "clear_failures": Mock(),
    }
    command = TransferHistoryMutationCommand(**dependencies)

    result = command.delete(7, delete_source=True, delete_destination=True)

    assert result.success is False
    assert result.destination.status == "failed"
    assert result.source.status == "deleted"
    assert result.history == "retained"
    repository.stage_delete.assert_not_called()
    dependencies["download_repository"].stage_delete_file_by_fullpath.assert_called_once_with(
        "/downloads/demo.mkv"
    )
    dependencies["unit_of_work"].commit.assert_called_once_with()
    dependencies["publish_download_file_deleted"].assert_called_once()
    dependencies["clear_failures"].assert_not_called()


def test_transfer_delete_treats_missing_requested_file_as_completed():
    """重试时已被前一次尝试删除的文件应跳过外部删除并允许清理历史。"""
    command, dependencies = _transfer_command(history=_history())
    dependencies["delete_media_file"].reset_mock()
    dependencies["file_exists"] = Mock(return_value=False)
    command = TransferHistoryMutationCommand(
        repository=dependencies["repository"],
        download_repository=dependencies["download_repository"],
        transfer_execution_repository=dependencies[
            "transfer_execution_repository"
        ],
        unit_of_work=dependencies["unit_of_work"],
        file_item_factory=dependencies["file_item_factory"],
        file_exists=dependencies["file_exists"],
        delete_media_file=dependencies["delete_media_file"],
        publish_download_file_deleted=dependencies["publish_download_file_deleted"],
        clear_failures=dependencies["clear_failures"],
    )

    result = command.delete(7, delete_source=True, delete_destination=True)

    assert result.success is True
    assert result.source.status == "already_missing"
    assert result.destination.status == "already_missing"
    dependencies["delete_media_file"].assert_not_called()
