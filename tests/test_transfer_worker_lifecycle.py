"""文件整理 worker 与 pending 回放的宿主生命周期测试。"""

import asyncio
import queue
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.transfer.workflow import (
    JobManager,
    TransferAdmission,
    TransferPlanningInput,
    TransferQueue,
    TransferTask,
)
from app.chain.transfer import TransferChain  # pylint: disable=no-name-in-module
from app.chain.transfer.execution import _TransferManualReviewRequired
from app.foundation.singleton import Singleton
from app.runtime.config import global_vars
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.startup.initializers import transfer as transfer_initializer
from tests.test_transfer_job_manager import make_task


def _planning_input(fileitem: FileItem) -> TransferPlanningInput:
    """构造 worker 准入与 claim 投影使用的真实规划输入。"""
    return TransferPlanningInput(
        source_fileitem=fileitem.model_dump(mode="json"),
        meta=None,
        mediainfo=None,
    )


def _build_chain(*, transfer_threads: int = 0) -> TransferChain:
    """构造只包含后台线程生命周期字段的 TransferChain 测试骨架。"""
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(
        transfer_threads=transfer_threads,
        transfer_task_timeout=0,
        video_extensions=(".mkv",),
        subtitle_extensions=(".srt",),
        audio_extensions=(".flac",),
    )
    chain._queue = queue.Queue()
    chain._transfer_interval = 0.1
    chain._threads = []
    chain._retiring_threads = []
    chain._queue_active = False
    chain._worker_stop_event = threading.Event()
    chain._worker_lifecycle_lock = threading.RLock()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._replay_thread = None
    chain._replay_stop_event = threading.Event()
    chain._recovery_wakeup_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._worker_owner_id = "worker-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    admissions = MagicMock()
    admissions.admit.side_effect = lambda **kwargs: TransferAdmission(
        task_id="admitted-task",
        storage=kwargs["storage"],
        src_path=kwargs["src_path"],
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=kwargs["planning_input"],
    )
    admissions.claim_task.side_effect = lambda **kwargs: TransferAdmission(
        task_id=kwargs["task_id"],
        storage="local",
        src_path="/downloads/test.mkv",
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=_planning_input(FileItem(
            storage="local",
            path="/downloads/test.mkv",
            type="file",
        )),
        lease_owner=kwargs["owner_id"],
        lease_token=f"lease-{kwargs['task_id']}",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )
    admissions.abandon_unstarted.return_value = 1
    admissions.release_claim.return_value = True
    chain._transfer_admissions = admissions
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    return chain


def _claimed_admission(task: TransferTask, task_id: str) -> TransferAdmission:
    """构造属于测试进程 owner 的有效 claim 投影。"""
    return TransferAdmission(
        task_id=task_id,
        storage=task.fileitem.storage,
        src_path=task.fileitem.path,
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=_planning_input(task.fileitem),
        lease_owner="worker-owner",
        lease_token=f"lease-{task_id}",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )


@pytest.fixture
def blocked_queue_admission(request: pytest.FixtureRequest) -> Iterator[SimpleNamespace]:
    """在准入数据库边界暂停生产者，并保留可按事件触发的真实续租线程。"""
    chain = _build_chain()
    chain.jobview = MagicMock()
    chain.jobview.add_task.return_value = True
    chain._register_scrape_batch_task = MagicMock()
    task = TransferTask(fileitem=FileItem(
        storage="local", path="/downloads/test.mkv", type="file",
    ))
    task.bind_planning_input(_planning_input(task.fileitem))
    current = _claimed_admission(task, "existing-task")
    initial_deadline = time.monotonic() + 60
    chain._owned_leases[current.task_id] = (current.lease_token, initial_deadline)
    chain._transfer_admissions.heartbeat.return_value = current
    database_entered = threading.Event()
    release_database = threading.Event()
    heartbeat_requested = threading.Event()
    heartbeat_finished = threading.Event()
    producer_results: list[bool] = []
    failures: list[Exception] = []
    database_method = getattr(chain._transfer_admissions, request.param)
    original_operation = database_method.side_effect

    def block_database(**kwargs) -> TransferAdmission:
        """保持仓储调用未返回，直到测试完成并发行为断言后显式放行。"""
        database_entered.set()
        release_database.wait()
        return original_operation(**kwargs)

    def enqueue_task() -> None:
        """从独立生产者执行真实入队路径，并回传结果与异常。"""
        try:
            producer_results.append(chain.put_to_queue(task))
        except Exception as error:
            failures.append(error)

    def renew_lease() -> None:
        """收到事件后执行真实续租，再等待宿主关闭信号。"""
        heartbeat_requested.wait()
        try:
            chain._TransferChain__heartbeat_owned_leases()
        except Exception as error:
            failures.append(error)
        finally:
            heartbeat_finished.set()
        chain._lease_heartbeat_stop_event.wait()

    database_method.side_effect = block_database
    producer = threading.Thread(target=enqueue_task, daemon=True)
    heartbeat = threading.Thread(target=renew_lease, daemon=True)
    chain._lease_heartbeat_thread = heartbeat
    state = SimpleNamespace(
        chain=chain,
        task=task,
        initial_deadline=initial_deadline,
        database_entered=database_entered,
        release_database=release_database,
        heartbeat_requested=heartbeat_requested,
        heartbeat_finished=heartbeat_finished,
        producer=producer,
        heartbeat=heartbeat,
        producer_results=producer_results,
        failures=failures,
        closer=None,
    )
    heartbeat.start()
    producer.start()
    try:
        assert database_entered.wait(timeout=1)
        yield state
    finally:
        release_database.set()
        heartbeat_requested.set()
        producer.join(timeout=1)
        if state.closer is not None:
            state.closer.join(timeout=1)
        chain.close_workers(timeout_seconds=1)
        chain._lease_heartbeat_stop_event.set()
        heartbeat.join(timeout=1)
        assert producer.is_alive() is False
        assert heartbeat.is_alive() is False
        assert state.closer is None or not state.closer.is_alive()


@pytest.mark.parametrize("blocked_queue_admission", ["admit", "claim_task"], indirect=True)
def test_slow_queue_admission_does_not_block_owned_lease_heartbeat(
        blocked_queue_admission: SimpleNamespace,
) -> None:
    """新任务数据库阻塞期间，已有任务必须独立续租并刷新本地期限。"""
    state = blocked_queue_admission
    state.heartbeat_requested.set()

    assert state.heartbeat_finished.wait(timeout=0.5)
    assert state.release_database.is_set() is False
    assert state.producer.is_alive() is True
    assert state.failures == []
    assert state.chain._owned_leases["existing-task"][1] > state.initial_deadline

    state.release_database.set()
    state.producer.join(timeout=1)
    assert state.producer_results == [True]
    assert state.chain._queue.get_nowait().task is state.task


@pytest.mark.parametrize("blocked_queue_admission", ["admit", "claim_task"], indirect=True)
def test_close_during_queue_admission_respects_budget_and_keeps_heartbeat(
        blocked_queue_admission: SimpleNamespace,
) -> None:
    """关闭不得越过阻塞生产者释放租约，超时后续租和已接收的入队仍可完成。"""
    state = blocked_queue_admission
    close_finished = threading.Event()
    close_results: list[tuple[bool, float]] = []

    def close_chain() -> None:
        """独立执行关闭，使旧实现无界等锁时测试仍能显式释放生产者。"""
        started_at = time.monotonic()
        try:
            result = state.chain.close_workers(timeout_seconds=0.02)
            close_results.append((result, time.monotonic() - started_at))
        finally:
            close_finished.set()

    state.closer = threading.Thread(target=close_chain, daemon=True)
    state.closer.start()

    assert close_finished.wait(timeout=0.5)
    assert close_results[0][0] is False
    assert close_results[0][1] < 0.5
    assert state.chain._lease_heartbeat_stop_event.is_set() is False
    assert state.heartbeat.is_alive() is True
    state.heartbeat_requested.set()
    assert state.heartbeat_finished.wait(timeout=0.5)
    assert state.chain._owned_leases["existing-task"][1] > state.initial_deadline
    assert state.release_database.is_set() is False

    state.release_database.set()
    state.producer.join(timeout=1)
    assert state.producer_results == [True]
    assert state.failures == []
    assert state.chain.close_workers(timeout_seconds=1) is True


@pytest.mark.parametrize("owner", ["worker", "replay"])
def test_closing_owner_rejects_enqueue_without_waiting_for_lifecycle_lock(owner: str) -> None:
    """已经等待准入锁的 owner 必须察觉关闭并退出，不能与关闭的 join 反等。"""
    chain = _build_chain()
    lifecycle_lock = threading.RLock()
    enqueue_waiting_for_lock = threading.Event()
    enqueue_results: list[bool] = []
    enqueue_failures: list[Exception] = []

    def acquire_lifecycle_lock(*, timeout: float) -> bool:
        """只观测 owner 已经开始等锁，实际竞争和主线程重入均交给真实 RLock。"""
        if threading.current_thread() is thread:
            enqueue_waiting_for_lock.set()
        return lifecycle_lock.acquire(timeout=timeout)

    def enqueue_before_close() -> None:
        """在关闭尚未开始时进入准入并阻塞于主线程持有的生命周期锁。"""
        try:
            enqueue_results.append(chain.put_to_queue(MagicMock()))
        except Exception as error:
            enqueue_failures.append(error)

    chain._worker_lifecycle_lock = SimpleNamespace(
        acquire=acquire_lifecycle_lock,
        release=lifecycle_lock.release,
    )
    thread = threading.Thread(target=enqueue_before_close, daemon=True)
    if owner == "worker":
        chain._threads = [thread]
    else:
        chain._replay_thread = thread
    lifecycle_lock.acquire()
    thread.start()
    try:
        assert enqueue_waiting_for_lock.wait(timeout=1)
        assert chain._closing is False
        started_at = time.monotonic()
        assert chain.close_workers(timeout_seconds=1) is True
        assert time.monotonic() - started_at < 0.5
        assert enqueue_results == [False]
        assert enqueue_failures == []
        chain._transfer_admissions.admit.assert_not_called()
        chain._transfer_admissions.claim_task.assert_not_called()
    finally:
        lifecycle_lock.release()
        thread.join(timeout=1)
        chain.close_workers(timeout_seconds=1)
        assert thread.is_alive() is False


def test_config_reload_replaces_worker_generation_and_keeps_accepting() -> None:
    """热更新应等待旧 worker 收敛，再启动使用独立停止信号的新一代。"""
    chain = _build_chain(transfer_threads=1)
    started_workers: queue.Queue = queue.Queue()

    def run_worker(stop_event: threading.Event) -> None:
        """记录 worker 代际并等待该代专属停止信号。"""
        started_workers.put((threading.current_thread(), stop_event))
        stop_event.wait()

    chain._TransferChain__start_transfer = run_worker
    assert chain._TransferChain__init() is True
    first_thread, first_stop_event = started_workers.get(timeout=1)

    chain.on_config_changed()

    second_thread, second_stop_event = started_workers.get(timeout=1)
    assert first_stop_event.is_set() is True
    assert first_thread.is_alive() is False
    assert second_thread is not first_thread
    assert second_stop_event is not first_stop_event
    assert second_stop_event.is_set() is False

    service = MagicMock()
    service.put.return_value = True
    chain._transfer_queue_service = MagicMock(return_value=service)
    task = MagicMock()
    assert chain.put_to_queue(task) is True
    service.put.assert_called_once()

    assert chain.close_workers(timeout_seconds=1) is True
    assert second_thread.is_alive() is False


def test_config_reload_hands_queue_to_new_generation_while_old_io_finishes() -> None:
    """旧代同步 I/O 超时不应让后续队列永久失去 worker。"""
    chain = _build_chain(transfer_threads=1)
    chain._WORKER_RESTART_TIMEOUT_SECONDS = 0.01
    started_workers: queue.Queue = queue.Queue()
    release_old_worker = threading.Event()
    invocation_count = 0
    invocation_lock = threading.Lock()

    def run_worker(stop_event: threading.Event) -> None:
        """首代模拟不可取消 I/O，后续代按各自停止信号正常收敛。"""
        nonlocal invocation_count
        with invocation_lock:
            generation = invocation_count
            invocation_count += 1
        started_workers.put((threading.current_thread(), stop_event))
        if generation == 0:
            release_old_worker.wait()
        else:
            stop_event.wait()

    chain._TransferChain__start_transfer = run_worker
    assert chain._TransferChain__init() is True
    old_thread, old_stop_event = started_workers.get(timeout=1)

    chain.on_config_changed()

    new_thread, new_stop_event = started_workers.get(timeout=1)
    assert old_stop_event.is_set() is True
    assert old_thread.is_alive() is True
    assert chain._retiring_threads == [old_thread]
    assert chain._threads == [new_thread]
    assert new_stop_event.is_set() is False

    release_old_worker.set()
    assert chain.close_workers(timeout_seconds=1) is True
    assert old_thread.is_alive() is False
    assert new_thread.is_alive() is False


def test_close_workers_is_bounded_and_retains_nonconverging_owner() -> None:
    """同步 I/O 线程超时后应保留句柄并报告失败，不能伪装成已取消。"""
    chain = _build_chain()
    release = threading.Event()
    thread = threading.Thread(
        target=release.wait,
        name="transfer-blocked-test",
        daemon=True,
    )
    chain._threads = [thread]
    thread.start()

    started_at = time.monotonic()
    assert chain.close_workers(timeout_seconds=0.01) is False
    assert time.monotonic() - started_at < 0.5
    assert chain._threads == []
    assert chain._retiring_threads == [thread]
    assert thread.is_alive() is True

    service = MagicMock()
    chain._transfer_queue_service = MagicMock(return_value=service)
    assert chain.put_to_queue(MagicMock()) is False
    service.put.assert_not_called()

    release.set()
    assert chain.close_workers(timeout_seconds=1) is True
    assert chain.close_workers(timeout_seconds=0) is True
    assert chain._threads == []
    assert chain._retiring_threads == []


def test_close_workers_lock_wait_uses_the_same_timeout_budget() -> None:
    """生命周期锁竞争必须耗用关闭预算，超时返回后不得延迟修改 worker 状态。"""
    chain = _build_chain()
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lifecycle_lock() -> None:
        """在独立线程持锁，稳定制造无法重入的生命周期锁竞争。"""
        with chain._worker_lifecycle_lock:
            lock_acquired.set()
            assert release_lock.wait(timeout=1)

    holder = threading.Thread(target=hold_lifecycle_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=1)

    started_at = time.monotonic()
    assert chain.close_workers(timeout_seconds=0.01) is False
    assert time.monotonic() - started_at < 0.5
    assert chain._closing is False
    assert chain._worker_stop_event.is_set() is False
    assert chain._queue.empty() is True

    release_lock.set()
    holder.join(timeout=1)
    assert holder.is_alive() is False
    assert chain.close_workers(timeout_seconds=1) is True


def test_close_keeps_failure_notification_when_workers_do_not_converge() -> None:
    """活跃整理线程超时后，失败通知 owner 必须继续供线程使用。"""
    chain = _build_chain()
    chain.close_workers = MagicMock(return_value=False)
    chain.failure_notification_aggregator = MagicMock()

    completed = asyncio.run(chain.close(timeout_seconds=0.01))

    assert completed is False
    chain.close_workers.assert_called_once_with(0.01)
    chain.failure_notification_aggregator.close.assert_not_called()


def test_close_releases_failure_notification_after_workers_converge() -> None:
    """worker 和回放退出后，整理链应刷新并关闭失败通知 owner。"""
    chain = _build_chain()
    chain.close_workers = MagicMock(return_value=True)
    chain.failure_notification_aggregator = MagicMock()

    completed = asyncio.run(chain.close(timeout_seconds=0.01))

    assert completed is True
    chain.failure_notification_aggregator.close.assert_called_once_with()


def test_stop_transfer_runtime_does_not_construct_chain(monkeypatch) -> None:
    """关闭入口在整理链从未使用时应直接成功，不能因关停而启动 worker。"""
    get_existing_instance = MagicMock(return_value=None)
    monkeypatch.setattr(
        transfer_initializer.TransferChain,
        "get_existing_instance",
        get_existing_instance,
    )

    completed = asyncio.run(
        transfer_initializer.stop_transfer_runtime(timeout_seconds=0.01)
    )

    assert completed is True
    get_existing_instance.assert_called_once_with()


def test_stop_transfer_runtime_closes_existing_chain(monkeypatch) -> None:
    """关闭入口应把超时预算和真实收敛结果原样传给既有整理链。"""
    chain = MagicMock(close=AsyncMock(return_value=False))
    monkeypatch.setattr(
        transfer_initializer.TransferChain,
        "get_existing_instance",
        MagicMock(return_value=chain),
    )

    completed = asyncio.run(
        transfer_initializer.stop_transfer_runtime(timeout_seconds=0.01)
    )

    assert completed is False
    chain.close.assert_awaited_once_with(timeout_seconds=0.01)


def test_constructor_failure_publishes_started_worker_to_cleanup(monkeypatch) -> None:
    """首个 worker 启动后构造失败时，stop-only 入口仍必须找到并等待它。"""
    instances = dict(Singleton._instances)
    instances.pop((TransferChain, (), frozenset()), None)
    monkeypatch.setattr(Singleton, "_instances", instances)
    worker_started = threading.Event()
    worker_release = threading.Event()
    workers: list[threading.Thread] = []

    def failing_init(chain: TransferChain) -> None:
        """模拟第二个 owner 启动失败前已经成功启动一个整理线程。"""
        worker = threading.Thread(
            target=lambda: (worker_started.set(), worker_release.wait()),
            name="transfer-partial-construction",
            daemon=True,
        )
        workers.append(worker)
        worker.start()

        async def close(*, timeout_seconds: float) -> bool:
            """模拟真实 close 释放并等待半构造实例已经发布的 worker。"""
            worker_release.set()
            worker.join(timeout=timeout_seconds)
            return not worker.is_alive()

        chain.close = close
        raise RuntimeError("second worker failed")

    monkeypatch.setattr(TransferChain, "__init__", failing_init)

    with pytest.raises(RuntimeError, match="second worker failed"):
        TransferChain()
    assert worker_started.wait(timeout=1)
    retained = TransferChain.get_existing_instance()
    assert retained is not None

    assert asyncio.run(
        transfer_initializer.stop_transfer_runtime(timeout_seconds=1)
    ) is True
    assert workers[0].is_alive() is False


def test_worker_requeues_item_taken_during_shutdown(monkeypatch) -> None:
    """停止信号与 queue.get 竞态时，未开始处理的任务必须原样放回队列。"""
    chain = _build_chain()
    work_queue = MagicMock()
    chain._queue = work_queue
    entered_get = threading.Event()
    release_get = threading.Event()
    item = TransferQueue()

    def get_item(*_args, **_kwargs):
        """让停止信号稳定落在阻塞取队列之后、任务处理之前。"""
        entered_get.set()
        assert release_get.wait(timeout=1)
        return item

    work_queue.get.side_effect = get_item
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())
    stop_event = threading.Event()
    thread = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    thread.start()
    assert entered_get.wait(timeout=1)

    stop_event.set()
    release_get.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    work_queue.put.assert_called_once_with(item)
    work_queue.task_done.assert_called_once_with()


def test_worker_settles_progress_when_only_stop_sentinel_remains(monkeypatch) -> None:
    """真实任务完成时仅剩停止哨兵，仍应结束进度并重置本批计数。"""
    chain = _build_chain()
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/downloads/movie.mkv",
            type="file",
            name="movie.mkv",
            basename="movie",
            extension="mkv",
        )
    )
    chain.jobview = MagicMock()
    chain.jobview.pending_total.return_value = 1
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    task_started = threading.Event()
    release_task = threading.Event()

    def handle_transfer(*_args, **_kwargs):
        """阻塞真实任务，让测试能在其完成前稳定插入停止哨兵。"""
        task_started.set()
        assert release_task.wait(timeout=1)
        return True, ""

    chain._TransferChain__handle_transfer = handle_transfer
    chain._TransferChain__start_job_execution = MagicMock()
    chain._TransferChain__finish_job_execution = MagicMock()
    chain._queue.put(TransferQueue(task=task))
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())
    stop_event = threading.Event()
    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    assert task_started.wait(timeout=1)

    stop_event.set()
    chain._queue.put(chain._QUEUE_STOP_SENTINEL)
    release_task.set()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    chain._progress.end.assert_called_once_with()
    assert chain._active_tasks == 0
    assert chain._total_num == 0
    assert chain._processed_num == 0
    assert chain._fail_num == 0
    with chain._queue.mutex:
        assert list(chain._queue.queue) == [chain._QUEUE_STOP_SENTINEL]


def test_durable_task_identity_flows_to_unsettled_terminal_claim_release(monkeypatch) -> None:
    """终态无原子回执时稳定身份必须用于释放 claim，pending 保持可恢复。"""
    chain = _build_chain()
    chain.runtime_config.transfer_task_timeout = 0
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/durable.mkv",
        type="file",
        name="durable.mkv",
        basename="durable",
        extension="mkv",
    ))
    discarded = threading.Event()
    admissions = MagicMock()
    admissions.admit.return_value = TransferAdmission(
        task_id="durable-task-id",
        storage="local",
        src_path=task.fileitem.path,
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=_planning_input(task.fileitem),
    )
    admissions.claim_task.return_value = _claimed_admission(
        task,
        "durable-task-id",
    )
    admissions.release_claim.side_effect = (
        lambda **_kwargs: discarded.set() or 1
    )
    chain._transfer_admissions = admissions
    chain.jobview = MagicMock()
    chain.jobview.add_task.return_value = True
    chain.jobview.pending_total.return_value = 1
    chain._register_scrape_batch_task = MagicMock()
    chain._finish_scrape_batch_task = MagicMock()
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    def complete_with_checkpoint(*, task, callback):
        """模拟真实 worker 只有提交 checkpoint 后才返回终态成功。"""
        task.bind_plan_checkpoint(MagicMock())
        return True, ""

    chain._TransferChain__handle_transfer = MagicMock(
        side_effect=complete_with_checkpoint
    )
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())

    assert chain.put_to_queue(task) is True
    assert task.lease_token == "lease-durable-task-id"
    admissions.claim_task.assert_called_once_with(
        task_id="durable-task-id",
        owner_id="worker-owner",
        lease_seconds=120,
    )
    stop_event = threading.Event()
    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    assert discarded.wait(timeout=1)
    stop_event.set()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert task.admission_task_id == "durable-task-id"
    admissions.claim_task.assert_called_once()
    admissions.release_claim.assert_called_once_with(
        task_id="durable-task-id",
        lease_token="lease-durable-task-id",
        error="整理终态未完成 durable 原子结算",
    )
    admissions.abandon_unstarted.assert_not_called()


def test_claimed_task_prevents_progress_settlement_before_active_registration() -> None:
    """其他 worker 已取走真实任务但尚未登记 active 时，当前批次不得提前结算。"""
    chain = _build_chain()
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/downloads/claimed.mkv",
            type="file",
            name="claimed.mkv",
            basename="claimed",
            extension="mkv",
        )
    )
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 1
    chain._fail_num = 0
    chain._total_num = 2
    claimed = threading.Event()
    release_claim = threading.Event()

    chain._queue.put(TransferQueue(task=task))

    def hold_claimed_task() -> None:
        """模拟 worker 已完成 queue.get、尚未取得 task_lock 登记 active 的窗口。"""
        item = chain._queue.get(timeout=1)
        assert item.task is task
        claimed.set()
        assert release_claim.wait(timeout=1)
        chain._queue.task_done()
        chain._TransferChain__settle_transfer_progress_if_idle()

    worker = threading.Thread(target=hold_claimed_task, daemon=True)
    worker.start()
    assert claimed.wait(timeout=1)

    chain._TransferChain__settle_transfer_progress_if_idle()

    chain._progress.end.assert_not_called()
    assert chain._processed_num == 1

    release_claim.set()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    chain._progress.end.assert_called_once_with()
    assert chain._total_num == 0
    assert chain._processed_num == 0


def test_replay_has_single_owner_and_close_waits_for_it() -> None:
    """重复回放只保留一个线程，关闭会通知并等待该线程退出。"""
    chain = _build_chain()
    del chain._TransferChain__ensure_recovery_scheduler
    replay_started = threading.Event()
    replay_calls = []

    def replay(stop_event: threading.Event) -> None:
        """模拟可由逐项检查点收敛的 pending 回放。"""
        replay_calls.append(stop_event)
        replay_started.set()
        stop_event.wait()

    chain._TransferChain__replay_pending = replay
    chain.replay_pending()
    assert replay_started.wait(timeout=1)
    replay_thread = chain._replay_thread

    chain.replay_pending()

    assert chain._replay_thread is replay_thread
    assert replay_calls == [chain._replay_stop_event]
    assert chain.close_workers(timeout_seconds=1) is True
    assert replay_thread.is_alive() is False
    assert chain._replay_thread is None


def test_recovered_worker_reuses_claimed_token_without_second_claim(
        monkeypatch,
) -> None:
    """恢复任务携带 token 入队后，普通 worker 必须直接执行而非二次 claim。"""
    chain = _build_chain()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/recovered.mkv",
        type="file",
        name="recovered.mkv",
        basename="recovered",
        extension="mkv",
    ))
    task.bind_admission_task_id("recovered-task")
    task.bind_execution_lease(
        owner_id="worker-owner",
        lease_token="lease-recovered-task",
    )
    chain._owned_leases = {
        "recovered-task": ("lease-recovered-task", time.monotonic() + 120)
    }
    chain.jobview = MagicMock()
    chain.jobview.pending_total.return_value = 1
    chain._finish_scrape_batch_task = MagicMock()
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    chain._transfer_admissions.release_claim.return_value = True
    stop_event = threading.Event()

    def complete_recovery(*, task, callback):
        """模拟恢复任务成功提交检查点并让 worker 在本项后退出。"""
        del callback
        task.bind_plan_checkpoint(MagicMock())
        stop_event.set()
        return True, ""

    chain._TransferChain__handle_transfer = complete_recovery
    chain._queue.put(TransferQueue(task=task))
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())

    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    chain._transfer_admissions.claim_task.assert_not_called()
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="recovered-task",
        lease_token="lease-recovered-task",
        error="整理终态未完成 durable 原子结算",
    )
    chain._transfer_admissions.abandon_unstarted.assert_not_called()


def test_worker_manual_review_logs_queue_guidance_without_traceback(monkeypatch) -> None:
    """后台 worker 遇到人工复核时只告警并释放内存执行状态，保留 durable 任务。"""
    chain = _build_chain()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/manual-review.mkv",
        type="file",
        name="manual-review.mkv",
        basename="manual-review",
        extension="mkv",
    ))
    task.bind_admission_task_id("manual-review-task")
    task.bind_execution_lease(
        owner_id="worker-owner",
        lease_token="lease-manual-review-task",
    )
    chain._owned_leases = {
        "manual-review-task": (
            "lease-manual-review-task",
            time.monotonic() + 120,
        ),
    }
    chain.jobview = MagicMock()
    chain.jobview.pending_total.return_value = 1
    chain._finish_scrape_batch_task = MagicMock()
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    stop_event = threading.Event()
    chain._transfer_admissions.release_claim.side_effect = (
        lambda **_kwargs: stop_event.set() or True
    )
    error = _TransferManualReviewRequired(
        "遗留步骤 operation-1 外部结果为 unknown，禁止自动重放"
    )
    chain._TransferChain__handle_transfer = MagicMock(side_effect=error)
    fail_task = MagicMock()
    chain._TransferChain__fail_transfer_task = fail_task
    warnings: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr("app.chain.transfer.workflow.logger.warning", warnings.append)
    monkeypatch.setattr("app.chain.transfer.workflow.logger.error", errors.append)
    chain._queue.put(TransferQueue(task=task))
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())

    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert len(warnings) == 1
    assert "打开整理队列" in warnings[0]
    assert errors == []
    fail_task.assert_called_once_with(task, error)
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="manual-review-task",
        lease_token="lease-manual-review-task",
        error=None,
    )
    assert chain._queue.unfinished_tasks == 0


def test_heartbeat_refreshes_current_token_and_forgets_lost_lease() -> None:
    """heartbeat 成功应刷新本地期限，CAS 拒绝后必须立即停止本地推进资格。"""
    chain = _build_chain()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/heartbeat.mkv",
        type="file",
    ))
    current = _claimed_admission(task, "heartbeat-task")
    initial_deadline = time.monotonic() + 1
    chain._owned_leases = {
        "heartbeat-task": ("lease-heartbeat-task", initial_deadline)
    }
    chain._transfer_admissions.heartbeat.return_value = current

    chain._TransferChain__heartbeat_owned_leases()

    assert chain._owned_leases["heartbeat-task"][1] > initial_deadline
    chain._transfer_admissions.heartbeat.return_value = None

    chain._TransferChain__heartbeat_owned_leases()

    assert "heartbeat-task" not in chain._owned_leases


def test_close_timeout_keeps_heartbeat_alive_until_blocked_worker_converges() -> None:
    """阻塞 worker 未退出时关闭不得停止 heartbeat 或允许租约过期接管。"""
    chain = _build_chain()
    worker_release = threading.Event()
    worker = threading.Thread(
        target=worker_release.wait,
        name="transfer-blocked-owner",
        daemon=True,
    )
    heartbeat = threading.Thread(
        target=chain._lease_heartbeat_stop_event.wait,
        name="transfer-heartbeat-owner",
        daemon=True,
    )
    chain._threads = [worker]
    chain._lease_heartbeat_thread = heartbeat
    chain._owned_leases = {
        "blocked-task": ("blocked-token", time.monotonic() + 120)
    }
    worker.start()
    heartbeat.start()

    assert chain.close_workers(timeout_seconds=0.01) is False
    assert heartbeat.is_alive() is True
    assert chain._lease_heartbeat_stop_event.is_set() is False

    worker_release.set()
    assert chain.close_workers(timeout_seconds=1) is True
    assert worker.is_alive() is False
    assert heartbeat.is_alive() is False
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="blocked-task",
        lease_token="blocked-token",
        error="整理宿主关闭，释放未结算任务租约",
    )


def test_worker_reports_failed_settlement_without_skipping_queue_bookkeeping(
        monkeypatch,
) -> None:
    """终态 CAS=0 必须计为失败，同时仍完成 task_done 与 active 归零。"""
    chain = _build_chain()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/stale.mkv",
        type="file",
        name="stale.mkv",
        basename="stale",
        extension="mkv",
    ))
    chain.jobview = MagicMock()
    chain.jobview.add_task.return_value = True
    chain.jobview.pending_total.return_value = 1
    chain._register_scrape_batch_task = MagicMock()
    chain._finish_scrape_batch_task = MagicMock()
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    chain._transfer_admissions.release_claim.return_value = False
    chain._TransferChain__settle_transfer_progress_if_idle = MagicMock()
    stop_event = threading.Event()

    def complete_with_stale_lease(*, task, callback):
        """模拟文件副作用完成后终态 token 已被新 owner 接管。"""
        del callback
        task.bind_plan_checkpoint(MagicMock())
        stop_event.set()
        return True, ""

    chain._TransferChain__handle_transfer = complete_with_stale_lease
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())
    assert chain.put_to_queue(task) is True

    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert chain._active_tasks == 0
    assert chain._fail_num == 1
    assert chain._queue.unfinished_tasks == 0


def test_failed_claim_release_waits_for_fixed_recovery_poll() -> None:
    """失败释放不得即时唤醒恢复线程，避免确定性错误形成热重试。"""
    chain = _build_chain()
    del chain._TransferChain__ensure_recovery_scheduler
    chain._RECOVERY_POLL_INTERVAL_SECONDS = 0.05
    chain._TransferChain__replay_pending = MagicMock()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/retry-later.mkv",
        type="file",
    ))
    task.bind_admission_task_id("retry-later")
    task.bind_execution_lease(
        owner_id="worker-owner",
        lease_token="retry-token",
    )
    chain._owned_leases = {
        "retry-later": ("retry-token", time.monotonic() + 120)
    }

    assert chain._TransferChain__release_task_claim(
        task,
        error="planning failed",
    ) is True

    assert chain._recovery_wakeup_event.is_set() is False
    assert chain._replay_thread is not None
    time.sleep(0.01)
    chain._TransferChain__replay_pending.assert_not_called()
    deadline = time.monotonic() + 0.5
    while (
            not chain._TransferChain__replay_pending.called
            and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    chain._TransferChain__replay_pending.assert_called()
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="retry-later",
        lease_token="retry-token",
        error="planning failed",
    )
    assert chain.close_workers(timeout_seconds=1) is True


def test_worker_fenced_releases_lost_lease_and_completes_queue_bookkeeping(
        monkeypatch,
) -> None:
    """本地租约失效时仍尝试 token CAS release，并完整结算内存队列。"""
    chain = _build_chain()
    task = make_task(1)
    task.bind_admission_task_id("lost-lease")
    task.bind_execution_lease(
        owner_id="worker-owner",
        lease_token="lost-token",
    )
    chain.jobview = JobManager()
    assert chain.jobview.add_task(task)
    assert chain.jobview.add_task(make_task(2))
    chain._finish_scrape_batch_task = MagicMock()
    chain._TransferChain__settle_transfer_progress_if_idle = MagicMock()
    stop_event = threading.Event()
    chain._transfer_admissions.release_claim.side_effect = (
        lambda **_kwargs: stop_event.set() or True
    )
    chain._queue.put(TransferQueue(task=task))
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())

    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="lost-lease",
        lease_token="lost-token",
        error="整理任务租约已经失效：lost-lease",
    )
    assert chain._queue.unfinished_tasks == 0
    assert chain._recovery_wakeup_event.is_set() is False
    assert chain.jobview.pending_total() == 1

    restart = make_task(1)
    restart.manual = True
    restart.bind_planning_input(_planning_input(restart.fileitem))
    chain._register_scrape_batch_task = MagicMock()
    assert chain.put_to_queue(restart) is True
    assert chain.jobview.pending_total() == 2
    assert chain._queue.get_nowait().task is restart
    chain._finish_queue_item(restart)


def test_callback_without_terminal_settlement_releases_claim_and_counts_failure(
        monkeypatch,
) -> None:
    """回调未给出原子结算回执时必须保留 pending、释放 claim 并计失败。"""
    chain = _build_chain()
    task = TransferTask(fileitem=FileItem(
        storage="local",
        path="/downloads/fenced-success.mkv",
        type="file",
        name="fenced-success.mkv",
    ))
    chain.jobview = MagicMock()
    chain.jobview.add_task.return_value = True
    chain.jobview.pending_total.return_value = 1
    chain._register_scrape_batch_task = MagicMock()
    chain._finish_scrape_batch_task = MagicMock()
    chain._progress = MagicMock()
    chain._active_tasks = 0
    chain._processed_num = 0
    chain._fail_num = 0
    chain._total_num = 0
    chain._transfer_admissions.release_claim.return_value = False
    chain._TransferChain__settle_transfer_progress_if_idle = MagicMock()
    success_callback = MagicMock(return_value=(True, ""))
    chain._TransferChain__default_callback = success_callback
    stop_event = threading.Event()

    def complete_with_success(*, task, callback):
        """模拟文件成功后进入受 durable 终态保护的回调。"""
        task.bind_plan_checkpoint(MagicMock())
        stop_event.set()
        return callback(
            task,
            TransferInfo(success=True, fileitem=task.fileitem),
        )

    chain._TransferChain__handle_transfer = complete_with_success
    monkeypatch.setattr(global_vars, "STOP_EVENT", threading.Event())
    assert chain.put_to_queue(task) is True

    worker = threading.Thread(
        target=chain._TransferChain__start_transfer,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    success_callback.assert_called_once()
    chain._transfer_admissions.release_claim.assert_called_once_with(
        task_id="admitted-task",
        lease_token="lease-admitted-task",
        error="整理终态未完成 durable 原子结算",
    )
    chain._transfer_admissions.abandon_unstarted.assert_not_called()
    assert chain._fail_num == 1
    assert chain._queue.unfinished_tasks == 0


def test_close_release_db_block_respects_deadline_and_keeps_heartbeat() -> None:
    """关闭租约释放被数据库阻塞时应按预算返回，并继续 heartbeat。"""
    chain = _build_chain()
    release_started = threading.Event()
    release_db = threading.Event()
    heartbeat = threading.Thread(
        target=chain._lease_heartbeat_stop_event.wait,
        name="transfer-heartbeat-release-test",
        daemon=True,
    )
    chain._lease_heartbeat_thread = heartbeat
    chain._owned_leases = {
        "blocked-release": ("blocked-token", time.monotonic() + 120)
    }

    def block_release(**_kwargs):
        """模拟数据库锁住 release_claim，直到测试显式放行。"""
        release_started.set()
        release_db.wait()
        return True

    chain._transfer_admissions.release_claim.side_effect = block_release
    heartbeat.start()

    started_at = time.monotonic()
    assert chain.close_workers(timeout_seconds=0.01) is False
    assert time.monotonic() - started_at < 0.5
    assert release_started.is_set()
    assert heartbeat.is_alive() is True
    assert chain._lease_heartbeat_stop_event.is_set() is False
    assert chain._lease_release_thread is not None
    assert chain._lease_release_thread.is_alive() is True

    release_db.set()
    assert chain.close_workers(timeout_seconds=1) is True
    assert heartbeat.is_alive() is False
    assert chain._lease_release_thread is None
