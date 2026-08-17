from unittest.mock import Mock

from app.application.transfer import TransferQueueService
from app.schemas.file import FileItem

from tests.test_transfer_job_manager import make_task


def _service(**overrides):
    """构造可观测整理队列服务及其默认依赖。"""
    dependencies = {
        "register_task": Mock(return_value=True),
        "enqueue": Mock(),
        "before_enqueue": Mock(),
        "after_enqueue": Mock(),
        "remove_task": Mock(),
        "list_tasks": Mock(return_value=["job"]),
        "expire_tasks": Mock(),
    }
    dependencies.update(overrides)
    return TransferQueueService(**dependencies), dependencies


def test_transfer_queue_service_put_preserves_registration_order():
    """入队必须先登记视图，再登记批次、写队列并落盘。"""
    calls = []
    service, _ = _service(
        register_task=lambda _task: calls.append("register") or True,
        before_enqueue=lambda _task: calls.append("batch"),
        enqueue=lambda _item: calls.append("queue"),
        after_enqueue=lambda _task: calls.append("pending"),
    )

    assert service.put(make_task(1), Mock()) is True
    assert calls == ["register", "batch", "queue", "pending"]


def test_transfer_queue_service_rejects_duplicate_without_side_effects():
    """作业视图拒绝重复任务后不得继续产生队列副作用。"""
    service, dependencies = _service(register_task=Mock(return_value=False))

    assert service.put(make_task(1), Mock()) is False
    dependencies["before_enqueue"].assert_not_called()
    dependencies["enqueue"].assert_not_called()
    dependencies["after_enqueue"].assert_not_called()


def test_transfer_queue_service_lists_and_removes_through_ports():
    """队列查询先清理失活任务，移除操作只委托作业视图。"""
    service, dependencies = _service()
    fileitem = FileItem(storage="local", path="/tmp/demo.mkv", type="file")

    assert service.list() == ["job"]
    service.remove(fileitem)

    dependencies["expire_tasks"].assert_called_once_with()
    dependencies["list_tasks"].assert_called_once_with()
    dependencies["remove_task"].assert_called_once_with(fileitem)
