"""
工作流命令及持久化端口构造；不注册测试用例。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.application.workflow import WorkflowMutationCommand


def _workflow(trigger_type="timer", timer="0 0 * * *", event_type="DownloadAdded"):
    """
    构造工作流写用例使用的最小快照。
    """
    return SimpleNamespace(
        id=7,
        trigger_type=trigger_type,
        timer=timer,
        event_type=event_type,
    )


def _command(workflow=None, commit_error=None):
    """
    构造可观察工作流事务与运行时副作用的命令。
    """
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
