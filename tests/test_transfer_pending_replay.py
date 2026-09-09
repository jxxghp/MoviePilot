"""
整理队列持久化与重启回放测试。

整理队列是纯内存的 queue.Queue：挂载挂死后的人工重启、版本升级、OOM、宿主
重启都会让队列连同「这些文件还没整理」这个事实一起蒸发，而已稳定落地的文件
不会再产生任何监控事件，也不会有新的补偿扫描起点——结果就是永久漏件。

这些测试固定三项不变量：入队即落盘登记、终态即注销、重启能回放。
"""
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.application.transfer.execution import (
    TransferExecutionSnapshot,
    TransferExecutionState,
)
from app.application.transfer.workflow import (
    TransferAdmission,
    TransferPlanningInput,
    TransferTask,
)
from app.chain.transfer import TransferChain  # pylint: disable=no-name-in-module
from app.schemas.file import FileItem


def _build_chain(admissions) -> TransferChain:
    """
    构造绕过单例初始化的 TransferChain 骨架。
    :param admissions: durable admission 仓储替身
    :return: TransferChain 骨架
    """
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = admissions
    chain._transfer_executions = MagicMock()
    chain._transfer_executions.get_snapshot.side_effect = (
        lambda *, task_id: _execution_snapshot(task_id=task_id)
    )
    chain._worker_owner_id = "test-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._worker_lifecycle_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._replay_stop_event = threading.Event()
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    return chain


def _execution_snapshot(
        *,
        task_id: str = "task-1",
        state: TransferExecutionState = TransferExecutionState.NOT_STARTED,
        steps: tuple[object, ...] = (),
) -> TransferExecutionSnapshot:
    """构造回放判定所需的最小执行状态投影。"""
    return TransferExecutionSnapshot(
        task_id=task_id,
        state=state,
        checkpoint=None,
        retry_generation=0,
        retry_count=0,
        retry_due_at=None,
        settlement_revision=0,
        terminal_history_id=None,
        last_error=None,
        steps=steps,
    )


def _admission(path: str, task_id: str = "task-1") -> TransferAdmission:
    """构造一条可脱离数据库会话使用的准入快照。"""
    planning_input = TransferPlanningInput(
        source_fileitem=_task(path).fileitem.model_dump(mode="json"),
        meta=None,
        mediainfo=None,
    )
    return TransferAdmission(
        task_id=task_id,
        storage="local",
        src_path=path,
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=planning_input,
        lease_owner="test-owner",
        lease_token=f"lease-{task_id}",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )


def _task(path: str, storage: str = "local") -> TransferTask:
    """
    构造测试用整理任务。
    :param path: 源文件路径
    :param storage: 存储
    :return: 整理任务
    """
    file_path = Path(path)
    return TransferTask(fileitem=FileItem(
        storage=storage,
        path=path,
        type="file",
        name=file_path.name,
        basename=file_path.stem,
        extension=file_path.suffix[1:],
    ))


def test_admit_transfer_records_storage_and_path():
    """
    入队时必须落盘登记源身份并在返回前取得执行租约。
    """
    admissions = MagicMock()
    admissions.admit.return_value = _admission(
        "/mnt/cd2/downloads/Movie.2024.mkv"
    )
    admissions.claim_task.return_value = _admission(
        "/mnt/cd2/downloads/Movie.2024.mkv"
    )
    chain = _build_chain(admissions)
    task = _task("/mnt/cd2/downloads/Movie.2024.mkv")

    result = chain._TransferChain__admit_transfer(task)

    call = admissions.admit.call_args.kwargs
    assert call["storage"] == "local"
    assert call["src_path"] == "/mnt/cd2/downloads/Movie.2024.mkv"
    assert isinstance(call["planning_input"], TransferPlanningInput)
    assert call["planning_input"].source_fileitem["path"] == call["src_path"]
    assert result.task_id == "task-1"
    assert task.lease_token == "lease-task-1"
    admissions.claim_task.assert_called_once_with(
        task_id="task-1",
        owner_id="test-owner",
        lease_seconds=120,
    )


def test_terminal_without_settlement_releases_claim_and_keeps_pending():
    """缺少原子终态回执时必须释放租约并保留 pending 供恢复。"""
    admissions = MagicMock()
    admissions.release_claim.return_value = True
    chain = _build_chain(admissions)
    chain.jobview = MagicMock()
    task = _task("/mnt/cd2/downloads/Movie.2024.mkv")
    task.bind_admission_task_id("task-1")
    task.bind_execution_lease(owner_id="test-owner", lease_token="lease-task-1")
    chain._worker_owner_id = "test-owner"
    chain._owned_leases = {
        "task-1": ("lease-task-1", time.monotonic() + 120)
    }
    assert chain._TransferChain__finish_job_execution(
        task,
        terminal=True,
        terminal_settlement=None,
    ) is False

    admissions.release_claim.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
        error="整理终态未完成 durable 原子结算",
    )
    admissions.abandon_unstarted.assert_not_called()


def test_replay_resends_pending_files_to_transfer(tmp_path, monkeypatch):
    """
    重启回放：登记过的文件要重新送入整理链，恢复被内存队列蒸发的任务。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x" * 10)

    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [_admission(str(media))]
    chain = _build_chain(admissions)

    transferred = []
    monkeypatch.setattr(
        chain,
        "_execute_transfer",
        lambda **kw: transferred.append(kw["fileitem"]),
    )

    chain._TransferChain__replay_pending()

    assert len(transferred) == 1
    item = transferred[0]
    assert item.path == media.as_posix()
    assert item.storage == "local"
    assert item.type == "file"
    # 回放时重新读取当前大小，不依赖登记时的陈旧信息
    assert item.size == 10


def test_replay_discards_vanished_files(tmp_path):
    """
    源文件已消失的登记要注销，否则每次启动都会重复回放一个不存在的文件。
    """
    admissions = MagicMock()
    missing = tmp_path / "gone.mkv"
    admissions.claim_recoverable.return_value = [_admission(str(missing))]
    admissions.abandon_unstarted.return_value = 1
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain._execute_transfer.assert_not_called()
    admissions.abandon_unstarted.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
    )
    admissions.release_claim.assert_not_called()
    assert chain._owned_leases == {}


def test_replay_releases_claim_when_vanished_source_abandon_is_rejected(
        tmp_path,
) -> None:
    """注销 CAS 被执行证据拒绝时必须释放 claim，不能留下无人续期的毒任务。"""
    admissions = MagicMock()
    missing = tmp_path / "state-changed.mkv"
    admission = _admission(str(missing))
    admissions.claim_recoverable.return_value = [admission]
    admissions.abandon_unstarted.return_value = 0
    admissions.release_claim.return_value = True
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    admissions.abandon_unstarted.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
    )
    admissions.release_claim.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
        error="源已消失但任务状态已变化，保留登记供恢复",
    )
    assert chain._owned_leases == {}


@pytest.mark.parametrize(
    ("execution_state", "steps"),
    [
        (TransferExecutionState.NOT_STARTED, ()),
        (TransferExecutionState.RUNNING, ()),
        (TransferExecutionState.RETRY_WAIT, ()),
        (TransferExecutionState.NOT_STARTED, (SimpleNamespace(),)),
    ],
)
def test_replay_with_execution_evidence_uses_frozen_source_when_source_vanished(
        tmp_path,
        monkeypatch,
        execution_state,
        steps,
) -> None:
    """已有执行状态或步骤证据时不得用源消失推断任务可删除。"""
    missing = tmp_path / "already-moved.mkv"
    planning_input = TransferPlanningInput(
        source_fileitem=_task(str(missing)).fileitem.model_dump(mode="json"),
        meta=None,
        mediainfo=None,
        requested_transfer_type="move",
    )
    admission = replace(
        _admission(str(missing)),
        state="planned",
        planning_input=planning_input,
        checkpoint=MagicMock(),
    )
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [admission]
    chain = _build_chain(admissions)
    chain._transfer_executions.get_snapshot.return_value = _execution_snapshot(
        state=execution_state,
        steps=steps,
    )
    chain._transfer_executions.get_snapshot.side_effect = None
    queue_planned = MagicMock(return_value=True)
    monkeypatch.setattr(
        chain,
        "_TransferChain__queue_planned_replay",
        queue_planned,
    )

    def reject_stat(*_args, **_kwargs):
        """冻结恢复触碰源文件即判定测试失败。"""
        pytest.fail("已有执行证据的恢复不得探测已经消失的源文件")

    monkeypatch.setattr(Path, "stat", reject_stat)

    chain._TransferChain__replay_pending()

    queued_fileitem = queue_planned.call_args.args[0]
    assert queued_fileitem.path == str(missing)
    admissions.abandon_unstarted.assert_not_called()
    admissions.release_claim.assert_not_called()


def test_replay_keeps_registration_when_mount_unreadable(tmp_path, monkeypatch):
    """
    挂载未就绪时读取失败属于暂时性故障，登记必须保留，等下次启动或人工整理。

    这与「文件已消失」必须区别对待：把挂载抖动误判成文件消失就等于主动丢件。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x")

    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [_admission(str(media))]
    admissions.release_claim.return_value = True
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    def unreadable(self, *_args, **_kwargs):
        """
        模拟挂载未就绪时的 stat 失败。
        """
        raise OSError(107, "Transport endpoint is not connected")

    monkeypatch.setattr(Path, "stat", unreadable)

    chain._TransferChain__replay_pending()

    chain._execute_transfer.assert_not_called()
    admissions.abandon_unstarted.assert_not_called()
    admissions.release_claim.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
        error="恢复源文件暂时不可读取",
    )


def test_replay_restores_bluray_directory_type(tmp_path, monkeypatch):
    """
    蓝光原盘登记时保留尾部斜杠，回放必须还原成目录类型，否则会被当成单文件整理。
    """
    bluray = tmp_path / "Movie.2024.BluRay"
    bluray.mkdir()
    src_path = f"{bluray.as_posix()}/"

    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [_admission(src_path)]
    chain = _build_chain(admissions)

    transferred = []
    monkeypatch.setattr(
        chain,
        "_execute_transfer",
        lambda **kw: transferred.append(kw["fileitem"]),
    )

    chain._TransferChain__replay_pending()

    assert len(transferred) == 1
    assert transferred[0].type == "dir"
    assert transferred[0].path == src_path


def test_replay_is_noop_without_registrations():
    """
    没有登记时回放不应触碰整理链。
    """
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = []
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain._execute_transfer.assert_not_called()


def test_replay_survives_db_failure():
    """
    读取登记失败不能让启动流程报错。
    """
    admissions = MagicMock()
    admissions.claim_recoverable.side_effect = RuntimeError("db gone")
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain._execute_transfer.assert_not_called()


def test_replay_continues_after_single_file_failure(tmp_path, monkeypatch):
    """
    单个文件回放失败不能中断整批回放，否则一个坏文件会拖住所有漏件的恢复。
    """
    first = tmp_path / "A.mkv"
    second = tmp_path / "B.mkv"
    for item in (first, second):
        item.write_bytes(b"x")

    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [
        _admission(str(first), "task-1"),
        _admission(str(second), "task-2"),
    ]
    chain = _build_chain(admissions)

    handled = []

    def flaky(**kw):
        """
        第一个文件整理抛异常，第二个正常。
        """
        if kw["fileitem"].name == "A.mkv":
            raise RuntimeError("boom")
        handled.append(kw["fileitem"].name)

    monkeypatch.setattr(chain, "_execute_transfer", flaky)

    chain._TransferChain__replay_pending()

    assert handled == ["B.mkv"]


def test_replay_stop_keeps_unprocessed_registrations(tmp_path, monkeypatch):
    """
    宿主关闭后不得继续检查或注销下一条登记，未处理项留给下次启动回放。
    """
    first = tmp_path / "A.mkv"
    first.write_bytes(b"x")
    missing_second = tmp_path / "gone.mkv"
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [
        _admission(str(first), "task-1"),
        _admission(str(missing_second), "task-2"),
    ]
    chain = _build_chain(admissions)
    stop_event = threading.Event()
    transferred = []

    def transfer_first(**kwargs):
        """首条回放送入整理链后模拟宿主发出关闭信号。"""
        transferred.append(kwargs["fileitem"].path)
        stop_event.set()

    monkeypatch.setattr(chain, "_execute_transfer", transfer_first)

    chain._TransferChain__replay_pending(stop_event)

    assert transferred == [first.as_posix()]
    admissions.abandon_unstarted.assert_not_called()
    assert admissions.release_claim.call_count == 2


def test_replay_registers_entire_claimed_batch_before_first_source_stat(
        tmp_path,
        monkeypatch,
):
    """批量 claim 返回后必须先把全部 token 交给 heartbeat，再做逐条同步 I/O。"""
    first = _admission(str(tmp_path / "A.mkv"), "task-1")
    second = _admission(str(tmp_path / "B.mkv"), "task-2")
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [first, second]
    admissions.release_claim.return_value = True
    chain = _build_chain(admissions)

    def observe_owned_batch(*_args, **_kwargs):
        """首个 stat 前观察两个 token 已同时进入续期集合。"""
        assert set(chain._owned_leases) == {"task-1", "task-2"}
        return None, False

    monkeypatch.setattr(
        chain,
        "_TransferChain__build_replay_fileitem",
        observe_owned_batch,
    )

    chain._TransferChain__replay_pending()

    assert admissions.release_claim.call_count == 2
    assert chain._owned_leases == {}


def test_replay_releases_claim_when_jobview_rejects_recovered_task(
        tmp_path,
        monkeypatch,
):
    """恢复任务未进入队列时必须立即 release，不能靠租约自然过期。"""
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x")
    admission = _admission(str(media))
    admission = replace(admission, checkpoint=MagicMock())
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [admission]
    admissions.release_claim.return_value = True
    chain = _build_chain(admissions)
    warning = MagicMock()
    monkeypatch.setattr("app.chain.transfer.queue.logger.warning", warning)
    monkeypatch.setattr(
        chain,
        "_TransferChain__queue_planned_replay",
        MagicMock(return_value=False),
    )

    chain._TransferChain__replay_pending()

    admissions.release_claim.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
        error="恢复任务未进入内存队列",
    )
    warning.assert_called_once_with(
        "待整理文件回放未入队：claim 1 个，原因见前序日志与 transferpending.last_error",
    )
    assert chain._owned_leases == {}


def test_claimed_enqueue_failure_never_uses_unfenced_error_writer() -> None:
    """陈旧 token 入队失败只能 release_claim，不能覆盖新 owner 的 last_error。"""
    admissions = MagicMock()
    admissions.release_claim.return_value = False
    chain = _build_chain(admissions)
    chain._finish_scrape_batch_task = MagicMock()
    chain.replay_pending = MagicMock()
    task = _task("/downloads/stale-enqueue.mkv")
    task.bind_admission_task_id("stale-task")
    task.bind_execution_lease(
        owner_id="test-owner",
        lease_token="stale-token",
    )
    chain._owned_leases = {
        "stale-task": ("stale-token", time.monotonic() + 120)
    }

    chain._TransferChain__record_enqueue_failure(
        task,
        RuntimeError("queue closed"),
    )

    admissions.record_enqueue_failure.assert_not_called()
    admissions.release_claim.assert_called_once_with(
        task_id="stale-task",
        lease_token="stale-token",
        error="queue closed",
    )
    assert chain._owned_leases == {}


@pytest.mark.parametrize("state", ["waiting", "running", "failed", "completed"])
@pytest.mark.parametrize("active", [False, True])
def test_manual_restart_replaces_only_finished_inactive_jobview(state, active):
    """手动重做清理批次残留终态，仍在执行或等待的同源任务必须保留。"""
    import queue

    from app.application.transfer.workflow import JobManager
    from tests.test_transfer_job_manager import make_task

    chain = _build_chain(MagicMock())
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = MagicMock()
    previous = make_task(1)
    sibling = make_task(2)
    assert chain.jobview.add_task(previous, state=state)
    assert chain.jobview.add_task(sibling)
    if active:
        chain.jobview.start_execution(previous)
    restart = make_task(1)
    restart.manual = True
    admission = _admission(restart.fileitem.path)
    restart.bind_planning_input(admission.planning_input)
    chain._transfer_admissions.admit.return_value = admission
    chain._transfer_admissions.claim_task.return_value = admission

    accepted = chain.put_to_queue(restart)

    assert accepted is (state in {"failed", "completed"} and not active)
    assert chain._queue.qsize() == int(accepted)
    assert chain.jobview.total() == 2
    if accepted:
        assert chain._transfer_admissions.admit.call_args.kwargs["replace_inactive"] is True
        assert chain.jobview.pending_total() == 2
        chain._queue.get_nowait()
        chain._finish_queue_item(restart)
    else:
        chain._transfer_admissions.admit.assert_not_called()


@pytest.mark.parametrize("state", ["waiting", "running", "failed", "completed"])
def test_recovery_requeues_orphaned_jobview_without_losing_group(state):
    """残留视图没有真实执行者时必须恢复入队，并保留同组其他文件。"""
    import queue

    from app.application.transfer.workflow import JobManager
    from tests.test_transfer_job_manager import make_task

    chain = _build_chain(MagicMock())
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = MagicMock()
    old_task = make_task(1)
    sibling = make_task(2)
    assert chain.jobview.add_task(old_task, state=state)
    assert chain.jobview.add_task(sibling)
    recovered = make_task(1)
    recovered.bind_admission_task_id("task-1")
    recovered.bind_execution_lease(owner_id="test-owner", lease_token="lease-task-1")
    chain._owned_leases["task-1"] = ("lease-task-1", time.monotonic() + 60)

    assert chain.put_to_queue(recovered)
    assert chain._queue.qsize() == 1
    assert chain._queue.get_nowait().task is recovered
    assert chain.jobview.pending_total() == 2
    chain._transfer_admissions.release_claim.assert_not_called()
    assert chain._TransferChain__is_claimed_task_enqueued("task-1", "lease-task-1")


@pytest.mark.parametrize("running", [False, True])
def test_recovery_reuses_real_task_without_second_enqueue(running):
    """相同租约的真实任务已排队或被 worker 取走时，回放应幂等成功。"""
    import queue

    from app.application.transfer.workflow import JobManager
    from tests.test_transfer_job_manager import make_task

    chain = _build_chain(MagicMock())
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = MagicMock()
    task = make_task(1)
    task.bind_admission_task_id("task-1")
    task.bind_execution_lease(owner_id="test-owner", lease_token="lease-task-1")
    chain._owned_leases["task-1"] = ("lease-task-1", time.monotonic() + 60)
    assert chain.put_to_queue(task)
    if running:
        chain._queue.get_nowait()
        chain._queued_lease_tokens.clear()
    duplicate = task.model_copy(deep=True)
    duplicate.fileitem.path += "/"

    assert chain.put_to_queue(duplicate)
    assert chain._queue.qsize() == (0 if running else 1)
    chain._register_scrape_batch_task.assert_called_once_with(task)
    chain._transfer_admissions.release_claim.assert_not_called()
    if not running:
        chain._queue.get_nowait()
    chain._finish_queue_item(task)
    assert not chain._resident_tasks
    assert chain._queue.unfinished_tasks == 0


def test_recovery_does_not_reuse_a_different_execution_lease():
    """不能把旧执行者当作新租约已入队，也不能覆盖仍持有的真实任务。"""
    import queue

    from app.application.transfer.workflow import JobManager
    from tests.test_transfer_job_manager import make_task

    chain = _build_chain(MagicMock())
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = MagicMock()
    old = make_task(1)
    old.bind_admission_task_id("task-1")
    old.bind_execution_lease(owner_id="test-owner", lease_token="old")
    chain._owned_leases["task-1"] = ("old", time.monotonic() + 60)
    assert chain.put_to_queue(old)
    recovered = make_task(1)
    recovered.bind_admission_task_id("task-1")
    recovered.bind_execution_lease(owner_id="test-owner", lease_token="new")
    chain._owned_leases["task-1"] = ("new", time.monotonic() + 60)

    assert chain.put_to_queue(recovered) is False
    assert chain._queue.qsize() == 1
    assert chain._queue.get_nowait().task is old


def test_normal_enqueue_is_visible_to_recovery_without_readmission():
    """普通准入任务也登记真实队列凭证，恢复同一租约不再次准入或入队。"""
    import queue

    from app.application.transfer.workflow import JobManager
    from tests.test_transfer_job_manager import make_task

    admissions = MagicMock()
    chain = _build_chain(admissions)
    chain._queue = queue.Queue()
    chain.jobview = JobManager()
    chain._register_scrape_batch_task = MagicMock()
    task = make_task(1)
    admission = _admission(task.fileitem.path)
    admissions.admit.return_value = admission
    admissions.claim_task.return_value = admission
    task.bind_planning_input(admission.planning_input)

    assert chain.put_to_queue(task)
    assert chain.put_to_queue(task.model_copy())
    assert chain._queue.qsize() == 1
    admissions.admit.assert_called_once()
    admissions.claim_task.assert_called_once()
    chain._register_scrape_batch_task.assert_called_once()
