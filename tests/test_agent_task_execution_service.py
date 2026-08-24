"""AgentTask 执行服务的取消、终态和调度清理合同。"""

import asyncio
import threading
from collections.abc import Callable
from uuid import uuid4

import pytest

from app.application.agenttask import AgentTaskExecutionService
from app.db.adapters.transaction import TransactionalWriteRunner
from app.db.oper.agenttask import AgentTaskOper
from app.db.session import SessionFactory, async_session_scope
from app.db.worker import DatabaseWorker
from app.schemas.exception import DatabaseWorkerClosedError


def _add_task(prefix: str, *, trigger_type: str = "cron"):
    """创建一条与其他用例隔离的可执行任务。"""
    user_id = f"{prefix}-{uuid4().hex}"
    return AgentTaskOper().add(
        name=f"{prefix} 检查",
        content="检查资源并报告",
        trigger_type=trigger_type,
        cron_expression="0 * * * *" if trigger_type == "cron" else None,
        run_at="2099-01-01T00:00:00+08:00" if trigger_type == "date" else None,
        user_id=user_id,
        username="admin",
        session_id=f"session-{user_id}",
        channel=None,
        source="api",
        original_chat_id=None,
    )


def _build_service(
    worker: DatabaseWorker,
    repository: Callable[[object], object] | None = None,
) -> AgentTaskExecutionService:
    """按生产事务和 worker 边界构造独立服务。"""
    transaction = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    return AgentTaskExecutionService(
        repository=repository or (lambda session: AgentTaskOper(session)),
        async_executor=worker,
        sync_transaction=transaction.sync,
    )


async def _wait_for_worker(
    worker: DatabaseWorker,
    predicate: Callable[[], bool],
) -> None:
    """等待 worker 进入目标状态，超时由测试框架明确失败。"""
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"数据库 worker 未进入目标状态: {worker.snapshot()}")


@pytest.mark.anyio
async def test_cancelled_queued_claim_does_not_create_run() -> None:
    """认领尚未开始时取消，应撤销排队工作且不得产生运行记录。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    occupied = threading.Event()
    release = threading.Event()
    blocker = asyncio.create_task(worker.run(
        lambda: (occupied.set(), release.wait())
    ))
    await asyncio.to_thread(occupied.wait)
    task = _add_task("queued-cancel")
    service = _build_service(worker)

    claim = asyncio.create_task(service.claim(task.id))
    await _wait_for_worker(worker, lambda: worker.snapshot().queued == 1)
    claim.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await claim
    await blocker
    current = AgentTaskOper().get(task.id)
    assert current.last_status == "waiting"
    assert current.last_run_id is None
    assert AgentTaskOper().list_runs(task.id) == []
    await worker.shutdown()


@pytest.mark.anyio
async def test_cancelled_started_claim_is_compensated_before_return() -> None:
    """认领事务已开始时取消，返回前必须把已提交运行收口为失败。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    class BlockingRepository:
        """在认领已写入但事务尚未提交的位置制造取消窗口。"""

        def __init__(self, session: object) -> None:
            self._repository = AgentTaskOper(session)

        def __getattr__(self, name: str):
            return getattr(self._repository, name)

        def begin_run(self, *args, **kwargs):
            run = self._repository.begin_run(*args, **kwargs)
            started.set()
            release.wait()
            return run

    task = _add_task("started-cancel")
    service = _build_service(worker, BlockingRepository)
    claim = asyncio.create_task(service.claim(task.id))
    await asyncio.to_thread(started.wait)
    claim.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await claim
    current = AgentTaskOper().get(task.id)
    runs = AgentTaskOper().list_runs(task.id)
    assert current.last_status == "failed"
    assert current.last_result == "Agent 定时任务已取消"
    assert current.run_count == 1
    assert len(runs) == 1
    assert runs[0].status == "failed"
    await worker.shutdown()


@pytest.mark.anyio
async def test_claim_retries_transient_worker_overload() -> None:
    """一次性任务不得因触发瞬间容量已满而永久丢失。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    task = _add_task("claim-overload", trigger_type="date")
    service = _build_service(worker)

    occupied = threading.Event()
    release = threading.Event()
    blocker = asyncio.create_task(worker.run(
        lambda: (occupied.set(), release.wait())
    ))
    await asyncio.to_thread(occupied.wait)
    claim = asyncio.create_task(service.claim(task.id))
    await _wait_for_worker(worker, lambda: worker.snapshot().rejected > 0)
    assert claim.done() is False
    release.set()

    claimed = await claim
    await blocker
    assert claimed.run is not None
    current = AgentTaskOper().get(task.id)
    assert current.last_status == "running"
    assert current.last_run_id == claimed.run.run_id
    await service.finalize(claimed.run, success=True, result="完成")
    await worker.shutdown()


@pytest.mark.anyio
async def test_repeated_finalize_cancellation_waits_for_single_terminal_write() -> None:
    """重复取消不得打断已开始的终态事务或重复累计执行次数。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    class BlockingRepository:
        """在运行终态已写入但事务尚未提交的位置制造重复取消窗口。"""

        def __init__(self, session: object) -> None:
            self._repository = AgentTaskOper(session)

        def __getattr__(self, name: str):
            return getattr(self._repository, name)

        def finish_run_outcome(self, *args, **kwargs):
            outcome = self._repository.finish_run_outcome(*args, **kwargs)
            started.set()
            release.wait()
            return outcome

    task = _add_task("finish-cancel")
    claim_service = _build_service(worker)
    claimed = await claim_service.claim(task.id)
    assert claimed.run is not None
    service = _build_service(worker, BlockingRepository)
    finalize = asyncio.create_task(service.finalize(
        claimed.run,
        success=True,
        result="完成",
    ))
    await asyncio.to_thread(started.wait)
    finalize.cancel()
    await asyncio.sleep(0)
    finalize.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await finalize
    current = AgentTaskOper().get(task.id)
    runs = AgentTaskOper().list_runs(task.id)
    assert current.last_status == "success"
    assert current.run_count == 1
    assert len(runs) == 1
    assert runs[0].status == "success"
    await worker.shutdown()


@pytest.mark.anyio
async def test_cancelled_queued_finalize_waits_for_terminal_write() -> None:
    """终态事务仍在队列时取消，返回前也必须完成唯一一次收口。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    task = _add_task("queued-finish-cancel")
    service = _build_service(worker)
    claimed = await service.claim(task.id)
    assert claimed.run is not None

    occupied = threading.Event()
    release = threading.Event()
    blocker = asyncio.create_task(worker.run(
        lambda: (occupied.set(), release.wait())
    ))
    await asyncio.to_thread(occupied.wait)
    finalize = asyncio.create_task(service.finalize(
        claimed.run,
        success=True,
        result="完成",
    ))
    await _wait_for_worker(worker, lambda: worker.snapshot().queued == 1)
    finalize.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await finalize
    await blocker
    current = AgentTaskOper().get(task.id)
    runs = AgentTaskOper().list_runs(task.id)
    assert current.last_status == "success"
    assert current.run_count == 1
    assert len(runs) == 1
    assert runs[0].status == "success"
    await worker.shutdown()


@pytest.mark.anyio
async def test_finalize_retries_transient_worker_overload() -> None:
    """容量暂满时保留终态 owner，取得 admission 后再提交结果。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    task = _add_task("finish-overload")
    service = _build_service(worker)
    claimed = await service.claim(task.id)
    assert claimed.run is not None

    occupied = threading.Event()
    release = threading.Event()
    blocker = asyncio.create_task(worker.run(
        lambda: (occupied.set(), release.wait())
    ))
    await asyncio.to_thread(occupied.wait)
    finalize = asyncio.create_task(service.finalize(
        claimed.run,
        success=True,
        result="完成",
    ))
    await _wait_for_worker(worker, lambda: worker.snapshot().rejected > 0)
    assert finalize.done() is False
    release.set()

    outcome = await finalize
    await blocker
    assert outcome.run_finalized is True
    assert AgentTaskOper().get(task.id).last_status == "success"
    assert worker.snapshot().queued == 0
    assert worker.snapshot().running == 0
    await worker.shutdown()


@pytest.mark.anyio
async def test_closed_worker_does_not_finalize_or_remove_schedule() -> None:
    """持久化不可用时不得伪造终态或清理运行时调度。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    task = _add_task("closed-finalize", trigger_type="date")
    service = _build_service(worker)
    claimed = await service.claim(task.id)
    assert claimed.run is not None
    await worker.shutdown()
    removed: list[tuple[int, int]] = []

    with pytest.raises(DatabaseWorkerClosedError):
        await service.finalize(
            claimed.run,
            success=True,
            result="完成",
            scheduler_generation=3,
            remove_schedule=lambda task_id, generation, _run_id: (
                removed.append((task_id, generation)) or True
            ),
        )

    assert removed == []
    assert AgentTaskOper().get(task.id).last_status == "running"
