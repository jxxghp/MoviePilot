from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer.workflow import (
    TransferAdmission,
    TransferPlanningInput,
    TransferQueueService,
)
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.schemas.file import FileItem
from tests.test_transfer_job_manager import make_task, make_transfer_chain


def _planning_input(path: str = "/tmp/demo.mkv") -> TransferPlanningInput:
    """构造队列准入测试要求的显式版本化输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": path,
            "type": "file",
            "name": path.rsplit("/", 1)[-1],
        },
        meta=None,
        mediainfo=None,
    )


def _service(**overrides):
    """构造可观测整理队列服务及其默认依赖。"""
    dependencies = {
        "register_task": Mock(return_value=True),
        "admit_task": Mock(return_value=TransferAdmission(
            task_id="task-1",
            storage="local",
            src_path="/tmp/demo.mkv",
            state="accepted",
            created_at="2026-08-27 10:00:00",
            updated_at="2026-08-27 10:00:00",
            planning_input=_planning_input(),
        )),
        "enqueue": Mock(),
        "before_enqueue": Mock(),
        "enqueue_failed": Mock(),
        "remove_task": Mock(),
        "list_tasks": Mock(return_value=["job"]),
        "expire_tasks": Mock(),
    }
    dependencies.update(overrides)
    return TransferQueueService(**dependencies), dependencies


def test_transfer_queue_service_put_preserves_registration_order():
    """入队必须先登记视图和 durable admission，再登记批次并写队列。"""
    calls = []
    service, _ = _service(
        register_task=lambda _task: calls.append("register") or True,
        admit_task=lambda _task: calls.append("admit") or TransferAdmission(
            task_id="task-1",
            storage="local",
            src_path="/tmp/demo.mkv",
            state="accepted",
            created_at="2026-08-27 10:00:00",
            updated_at="2026-08-27 10:00:00",
            planning_input=_planning_input(),
        ),
        before_enqueue=lambda _task: calls.append("batch"),
        enqueue=lambda _item: calls.append("queue"),
    )

    task = make_task(1)
    assert service.put(task, Mock()) is True
    assert calls == ["register", "admit", "batch", "queue"]
    assert task.admission_task_id == "task-1"


def test_transfer_queue_service_rejects_duplicate_without_side_effects():
    """作业视图拒绝重复任务后不得继续产生队列副作用。"""
    service, dependencies = _service(register_task=Mock(return_value=False))

    assert service.put(make_task(1), Mock()) is False
    dependencies["before_enqueue"].assert_not_called()
    dependencies["enqueue"].assert_not_called()
    dependencies["admit_task"].assert_not_called()


def test_transfer_queue_service_blocks_enqueue_when_admission_fails():
    """持久化失败必须撤销作业视图，不能继续加入内存队列。"""
    service, dependencies = _service(
        admit_task=Mock(side_effect=RuntimeError("db locked")),
    )
    task = make_task(1)

    with pytest.raises(RuntimeError, match="db locked"):
        service.put(task, Mock())

    dependencies["remove_task"].assert_called_once_with(task.fileitem)
    dependencies["before_enqueue"].assert_not_called()
    dependencies["enqueue"].assert_not_called()


def test_transfer_queue_service_keeps_admission_when_enqueue_fails():
    """内存入队失败必须记录原因并清理视图，durable admission 由仓储保留。"""
    error = RuntimeError("queue closed")
    service, dependencies = _service(
        enqueue=Mock(side_effect=error),
    )
    task = make_task(1)

    with pytest.raises(RuntimeError, match="queue closed"):
        service.put(task, Mock())

    dependencies["enqueue_failed"].assert_called_once_with(task, error)
    dependencies["remove_task"].assert_called_once_with(task.fileitem)


def test_transfer_queue_service_cleans_up_when_batch_registration_fails():
    """准入后的批次登记异常也必须留痕并撤销作业视图。"""
    error = RuntimeError("batch registration failed")
    service, dependencies = _service(
        before_enqueue=Mock(side_effect=error),
    )
    task = make_task(1)

    with pytest.raises(RuntimeError, match="batch registration failed"):
        service.put(task, Mock())

    dependencies["enqueue_failed"].assert_called_once_with(task, error)
    dependencies["remove_task"].assert_called_once_with(task.fileitem)
    dependencies["enqueue"].assert_not_called()


def test_transfer_queue_service_commits_admission_before_failed_enqueue(tmp_path):
    """真实仓储已提交后即使内存入队失败，任务也必须带原因留待恢复。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'durable-admission.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    repository = TransactionalTransferAdmissionRepository(factory)
    task = make_task(1)
    task.bind_planning_input(_planning_input(task.fileitem.path))
    service, _ = _service(
        admit_task=lambda item: repository.admit(
            storage=item.fileitem.storage,
            src_path=item.fileitem.path,
            planning_input=item.planning_input,
        ),
        enqueue=Mock(side_effect=RuntimeError("queue closed")),
        enqueue_failed=lambda item, error: repository.record_enqueue_failure(
            task_id=item.admission_task_id,
            error=str(error),
        ),
    )

    with pytest.raises(RuntimeError, match="queue closed"):
        service.put(task, Mock())

    with factory() as session:
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == task.admission_task_id
            )
        ).scalar_one()
        assert pending.last_error == "queue closed"
    engine.dispose()


def test_transfer_queue_service_lists_and_removes_through_ports():
    """队列查询先清理失活任务，移除操作只委托作业视图。"""
    service, dependencies = _service()
    fileitem = FileItem(storage="local", path="/tmp/demo.mkv", type="file")

    assert service.list() == ["job"]
    service.remove(fileitem)

    dependencies["expire_tasks"].assert_called_once_with()
    dependencies["list_tasks"].assert_called_once_with()
    dependencies["remove_task"].assert_called_once_with(fileitem)


def test_do_transfer_reports_durable_admission_failure():
    """背景整理准入失败必须返回批次失败，不能伪装成重复任务成功。"""
    chain = make_transfer_chain()
    fileitem = make_task(1).fileitem
    chain._TransferChain__get_trans_fileitems = lambda _item, **_kwargs: [
        (fileitem, False)
    ]
    chain.put_to_queue = Mock(side_effect=RuntimeError("db locked"))
    no_history = SimpleNamespace(
        get_by_src=lambda _src, storage=None: None,
        get_success_by_src=lambda _src, storage=None: None,
    )
    no_download = SimpleNamespace(
        get_by_hash=lambda _hash: None,
        get_file_by_fullpath=lambda _path: None,
        get_files_by_savepath=lambda _path: [],
        get_by_path=lambda _path: None,
    )
    chain.transfer_history_repository = no_history
    chain.download_history_repository = no_download

    with patch(
        "app.chain.transfer.workflow.get_configured_system_config",
        return_value=SimpleNamespace(get=lambda _key: None),
    ):
        state, message = chain.do_transfer(fileitem=fileitem, background=True)

    assert state is False
    assert "未能加入整理队列，请稍后重试" in message
    assert "db locked" not in message
