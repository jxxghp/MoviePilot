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
from unittest.mock import MagicMock

from app.application.transfer.workflow import TransferAdmission, TransferPlanningInput, TransferTask
from app.chain.transfer import TransferChain
from app.schemas.file import FileItem


def _build_chain(admissions) -> TransferChain:
    """
    构造绕过单例初始化的 TransferChain 骨架。
    :param admissions: durable admission 仓储替身
    :return: TransferChain 骨架
    """
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = admissions
    chain._worker_owner_id = "test-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._replay_stop_event = threading.Event()
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    return chain


def _admission(path: str, task_id: str = "task-1") -> TransferAdmission:
    """构造一条可脱离数据库会话使用的准入快照。"""
    return TransferAdmission(
        task_id=task_id,
        storage="local",
        src_path=path,
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
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
    入队时必须落盘登记「存储 + 源路径」这一最小事实。
    """
    admissions = MagicMock()
    admissions.admit.return_value = _admission(
        "/mnt/cd2/downloads/Movie.2024.mkv"
    )
    chain = _build_chain(admissions)

    result = chain._TransferChain__admit_transfer(
        _task("/mnt/cd2/downloads/Movie.2024.mkv")
    )

    call = admissions.admit.call_args.kwargs
    assert call["storage"] == "local"
    assert call["src_path"] == "/mnt/cd2/downloads/Movie.2024.mkv"
    assert isinstance(call["planning_input"], TransferPlanningInput)
    assert call["planning_input"].source_fileitem["path"] == call["src_path"]
    assert result.task_id == "task-1"


def test_discard_pending_on_terminal_state():
    """
    整理到达终态后必须注销登记，否则每次重启都会重复回放。
    """
    admissions = MagicMock()
    chain = _build_chain(admissions)
    task = _task("/mnt/cd2/downloads/Movie.2024.mkv")
    task.bind_admission_task_id("task-1")
    task.bind_execution_lease(owner_id="test-owner", lease_token="lease-task-1")
    chain._worker_owner_id = "test-owner"
    chain._owned_leases = {
        "task-1": ("lease-task-1", time.monotonic() + 120)
    }
    admissions.discard_claimed.return_value = 1

    assert chain._TransferChain__discard_pending(task) is True

    admissions.discard_claimed.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
    )


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
    admissions.discard_claimed.return_value = 1
    chain = _build_chain(admissions)
    chain._execute_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain._execute_transfer.assert_not_called()
    admissions.discard_claimed.assert_called_once_with(
        task_id="task-1",
        lease_token="lease-task-1",
    )


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
    admissions.discard_claimed.assert_not_called()
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
    admissions.discard_claimed.assert_not_called()
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
