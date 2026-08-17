from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.history import (
    DownloadHistoryMutationCommand,
    TransferHistoryMutationCommand,
)


def _transfer_command(*, history=None, delete_result=True, commit_error=None):
    """构造可观察整理历史事务和外部副作用的命令。"""
    repository = Mock()
    repository.get.return_value = history
    download_repository = Mock()
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = commit_error
    dependencies = {
        "repository": repository,
        "download_repository": download_repository,
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
    dependencies["repository"].stage_truncate.assert_called_once_with()
    dependencies["unit_of_work"].commit.assert_called_once_with()
