from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.workflow import (
    WorkflowDefinitionCommand,
    WorkflowMutationCommand,
)


def _workflow(trigger_type="timer", timer="0 0 * * *", event_type="DownloadAdded"):
    """构造工作流写用例使用的最小快照。"""
    return SimpleNamespace(
        id=7,
        trigger_type=trigger_type,
        timer=timer,
        event_type=event_type,
    )


def _command(workflow=None, commit_error=None):
    """构造可观察工作流事务与运行时副作用的命令。"""
    repository = Mock()
    repository.get = Mock(return_value=workflow)
    repository.stage_state = Mock(return_value=True)
    repository.stage_update = Mock(return_value=workflow)
    repository.stage_delete = Mock()
    unit_of_work = Mock()
    unit_of_work.commit = Mock(side_effect=commit_error)
    unit_of_work.rollback = Mock()
    dependencies = {
        "repository": repository,
        "unit_of_work": unit_of_work,
        "add_timer": Mock(),
        "remove_timer": Mock(),
        "load_event": Mock(),
        "remove_event": Mock(),
        "refresh_event": Mock(),
        "stop_running": Mock(),
        "delete_cache": Mock(),
    }
    return WorkflowMutationCommand(**dependencies), dependencies


def test_start_timer_workflow_commits_before_registering_job():
    """启用定时工作流必须先提交 W 状态，再登记定时任务。"""
    calls = []
    command, dependencies = _command(_workflow())
    dependencies["unit_of_work"].commit.side_effect = lambda: calls.append("commit")
    dependencies["add_timer"].side_effect = lambda _workflow: calls.append("timer")

    result = command.start(7)

    assert result.success is True
    assert calls == ["commit", "timer"]
    dependencies["repository"].stage_state.assert_called_once_with(7, "W")


def test_start_rejects_invalid_trigger_without_transaction():
    """未知触发类型不得更新数据库或注册运行时触发器。"""
    command, dependencies = _command(_workflow(trigger_type="unknown"))

    result = command.start(7)

    assert result.success is False
    assert result.message == "工作流触发类型不支持"
    dependencies["unit_of_work"].commit.assert_not_called()


def test_pause_event_workflow_commits_before_runtime_cleanup():
    """停用事件工作流必须提交 P 状态后再移除事件和停止执行。"""
    calls = []
    command, dependencies = _command(_workflow(trigger_type="event", timer=None))
    dependencies["unit_of_work"].commit.side_effect = lambda: calls.append("commit")
    dependencies["remove_event"].side_effect = lambda *_args: calls.append("event")
    dependencies["stop_running"].side_effect = lambda _id: calls.append("stop")

    result = command.pause(7)

    assert result.success is True
    assert calls == ["commit", "event", "stop"]


def test_delete_commit_failure_rolls_back_without_runtime_side_effects():
    """删除提交失败必须回滚，且不得删除缓存或运行时触发器。"""
    command, dependencies = _command(
        _workflow(),
        commit_error=RuntimeError("commit failed"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        command.delete(7)

    dependencies["unit_of_work"].rollback.assert_called_once_with()
    dependencies["delete_cache"].assert_not_called()
    dependencies["remove_timer"].assert_not_called()


def test_update_refreshes_timer_and_event_after_commit():
    """更新工作流提交后重建定时器并刷新事件注册。"""
    workflow = _workflow()
    command, dependencies = _command(workflow)

    result = command.update({"id": 7, "name": "updated"})

    assert result.success is True
    dependencies["repository"].stage_update.assert_called_once()
    dependencies["unit_of_work"].commit.assert_called_once_with()
    dependencies["remove_timer"].assert_called_once_with(workflow)
    dependencies["add_timer"].assert_called_once_with(workflow)
    dependencies["refresh_event"].assert_called_once_with(workflow)


def _definition_command(*, existing=None, commit_error=None, report_fork=None):
    """构造可观察异步工作流定义事务的命令。"""
    repository = Mock()
    repository.async_get_by_name = AsyncMock(return_value=existing)
    repository.async_get = AsyncMock(return_value=existing)
    repository.stage_create = AsyncMock(return_value=SimpleNamespace(id=8))
    repository.stage_reset = AsyncMock(return_value=existing)
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock(side_effect=commit_error)
    unit_of_work.rollback = AsyncMock()
    dependencies = {
        "repository": repository,
        "unit_of_work": unit_of_work,
        "stop_running": Mock(),
        "delete_cache": Mock(),
        "report_fork": report_fork or AsyncMock(),
    }
    return WorkflowDefinitionCommand(**dependencies), dependencies


@pytest.mark.asyncio
async def test_create_workflow_applies_defaults_and_commits_once():
    """创建工作流由应用用例补齐默认状态并统一提交。"""
    command, dependencies = _definition_command()

    result = await command.create({"name": "Demo", "state": None})

    assert result.success is True
    payload = dependencies["repository"].stage_create.await_args.args[0]
    assert payload["trigger_type"] == "timer"
    assert payload["state"] == "P"
    assert payload["add_time"]
    dependencies["unit_of_work"].commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_create_duplicate_name_has_no_transaction():
    """名称重复时不得暂存或提交工作流。"""
    command, dependencies = _definition_command(existing=SimpleNamespace(id=1))

    result = await command.create({"name": "Demo"})

    assert result.success is False
    dependencies["repository"].stage_create.assert_not_awaited()
    dependencies["unit_of_work"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_fork_commits_before_reporting_remote_count():
    """共享工作流必须先本地提交，随后才更新远程复用次数。"""
    calls = []

    async def commit():
        calls.append("commit")

    async def report(_share_id):
        calls.append("report")

    command, dependencies = _definition_command(report_fork=report)
    dependencies["unit_of_work"].commit.side_effect = commit

    result = await command.fork(
        {
            "name": "Forked",
            "actions": "[]",
            "flows": "[]",
            "context": "{}",
            "event_conditions": "{}",
        },
        share_id=9,
    )

    assert result.success is True
    assert calls == ["commit", "report"]


@pytest.mark.asyncio
async def test_fork_invalid_json_stops_before_database_write():
    """共享内容 JSON 无效时不得创建半成品工作流。"""
    command, dependencies = _definition_command()

    result = await command.fork({"name": "Forked", "actions": "{"})

    assert result.success is False
    assert result.message == "actions字段JSON格式错误"
    dependencies["repository"].stage_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_commit_failure_does_not_stop_runtime_or_delete_cache():
    """重置提交失败时只回滚数据库，不影响现有运行态。"""
    command, dependencies = _definition_command(
        existing=_workflow(),
        commit_error=RuntimeError("commit failed"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await command.reset(7)

    dependencies["unit_of_work"].rollback.assert_awaited_once_with()
    dependencies["stop_running"].assert_not_called()
    dependencies["delete_cache"].assert_not_called()


@pytest.mark.asyncio
async def test_reset_commits_before_runtime_cleanup():
    """工作流重置成功后再停止执行并删除缓存。"""
    calls = []
    command, dependencies = _definition_command(existing=_workflow())
    dependencies["unit_of_work"].commit.side_effect = lambda: calls.append("commit")
    dependencies["stop_running"].side_effect = lambda _id: calls.append("stop")
    dependencies["delete_cache"].side_effect = lambda _id: calls.append("cache")

    result = await command.reset(7)

    assert result.success is True
    assert calls == ["commit", "stop", "cache"]
