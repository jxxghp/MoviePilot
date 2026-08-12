"""
挂载级(block 型)故障下的监控自愈测试。

背景：CloudDrive2/115 等 FUSE 挂载会进入「请求不返回错误、永不返回」的挂死
状态。此时监控自愈体系的检测环节仍然有效，但恢复环节（重建监控线程、重试
整理队列）本身要访问挂载，一旦内联执行就会把全局自愈的单点——健康检查——
冻死在它自己要修复的挂载上，随后停滞检测、告警、重试驱动全部静默失效。

crash 型（挂载抛 Transport endpoint is not connected）的自愈由
test_monitor_resilience.py 覆盖；这些测试固定 block 型的两项不变量：
「看门狗零挂载访问」与「挂载级故障隔离/探测/恢复」。
"""
import threading
import time
from pathlib import Path
from threading import Lock, Thread
from unittest.mock import MagicMock

import pytest

from app.monitor import LocalDirectoryWatcher, Monitor
from app.monitor.recovery import RecoveryExecutor, RecoveryState, probe_path


def _build_monitor(monkeypatch, put_recorder=None):
    """
    构造测试用 Monitor 骨架，绕过单例初始化。
    :param monkeypatch: pytest monkeypatch
    :param put_recorder: 消息推送记录器
    :return: Monitor 骨架
    """
    put_recorder = put_recorder or MagicMock()
    monkeypatch.setattr("app.monitor.monitor.MessageHelper", MagicMock(return_value=put_recorder))
    monitor = object.__new__(Monitor)
    monitor._dispatcher = MagicMock()
    monitor._watchers = []
    monitor._watcher_lock = Lock()
    monitor._pending_locals = []
    monitor._alerted_paths = {}
    monitor._restart_marks = {}
    monitor._stable_cycles = {}
    monitor._isolated = {}
    monitor._recovery = RecoveryExecutor()
    return monitor


def _fake_watcher(mon_path, alive=True, stalled=False, restart_count=0):
    """
    构造测试用监控线程替身。
    :param mon_path: 监控目录
    :param alive: 线程是否存活
    :param stalled: 是否静默失效
    :param restart_count: 自动重启次数
    :return: 监控线程替身
    """
    watcher = MagicMock()
    watcher.watch_path = mon_path
    watcher.is_alive.return_value = alive
    watcher.is_stalled.return_value = stalled
    watcher.restart_count = restart_count
    watcher.last_activity_time = time.time()
    return watcher


def _run_watchdog(monitor, timeout=10.0):
    """
    在独立线程里跑一次健康检查，返回它是否在限定时间内结束。
    :param monitor: Monitor 骨架
    :param timeout: 最长等待秒数
    :return: 健康检查是否已返回
    """
    done = threading.Event()

    def runner():
        """
        执行一次健康检查并标记结束。
        """
        try:
            monitor.watchdog()
        finally:
            done.set()

    Thread(target=runner, daemon=True, name="test-watchdog").start()
    return done.wait(timeout=timeout)


# --------------------------------------------------------------------------- #
# P0：看门狗线程零挂载访问
# --------------------------------------------------------------------------- #

def test_watchdog_returns_while_rebuild_blocks_forever(tmp_path, monkeypatch):
    """
    根因回归：重建动作卡在死挂载上永不返回时，健康检查本身必须在有限时间内
    返回。事故中看门狗内联执行重建，冻死后 13 个目录的停滞检测与告警全部失效。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 0.5)
    monitor = _build_monitor(monkeypatch)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]

    entered = threading.Event()
    release = threading.Event()

    def blocking_rebuild(_target):
        """
        模拟 block 型挂载：进入后永不返回。
        """
        entered.set()
        release.wait()

    setattr(monitor, "_Monitor__rebuild_watcher", blocking_rebuild)

    try:
        assert _run_watchdog(monitor), "看门狗被重建动作冻死，未能在限定时间内返回"
        assert entered.is_set(), "重建动作没有被真正发起"
    finally:
        release.set()


def test_watchdog_returns_while_pending_retry_blocks_forever(tmp_path, monkeypatch):
    """
    重试驱动（整理重试队列的 stat）同样会卡在死挂载上，也必须移出看门狗线程。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 0.5)
    monitor = _build_monitor(monkeypatch)
    release = threading.Event()
    monitor._dispatcher.retry_pending.side_effect = lambda: release.wait()

    try:
        assert _run_watchdog(monitor), "看门狗被整理重试驱动冻死，未能在限定时间内返回"
    finally:
        release.set()


def test_watchdog_returns_while_local_retry_blocks_forever(tmp_path, monkeypatch):
    """
    启动失败目录的重试会走 decide_monitor_mode 的 os.walk 与 watcher.start()
    的 exists()，同样触碰挂载，必须移出看门狗线程。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 0.5)
    monitor = _build_monitor(monkeypatch)
    monitor._pending_locals = [{"mon_path": tmp_path, "monitor_mode": "compatibility"}]
    release = threading.Event()

    def blocking_start(**_kwargs):
        """
        模拟启动重试卡在死挂载的目录遍历上。
        """
        release.wait()
        return False

    setattr(monitor, "_Monitor__start_local_monitor", blocking_start)

    try:
        assert _run_watchdog(monitor), "看门狗被本地监控重试冻死，未能在限定时间内返回"
    finally:
        release.set()


def test_check_watchers_performs_no_filesystem_access(tmp_path, monkeypatch):
    """
    检测与判定必须是纯内存运算：只读线程存活标志、心跳与重启计数，
    不做任何文件系统访问，否则检测环节本身也会被 block 型故障拖死。
    """
    monitor = _build_monitor(monkeypatch)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]

    def forbidden(*_args, **_kwargs):
        """
        任何真实的文件系统调用都应视为检测环节的缺陷。
        """
        raise AssertionError("检测环节不允许访问文件系统")

    for name in ("stat", "exists", "is_dir", "iterdir", "rglob"):
        monkeypatch.setattr(Path, name, forbidden, raising=False)

    broken = monitor._Monitor__check_watchers()

    assert broken == [watcher]


def test_watchdog_survives_blocking_exists_in_real_rebuild(tmp_path, monkeypatch):
    """
    端到端回归：不替换任何恢复逻辑，只让 Path.exists() 永不返回（等价于对 FUSE
    守护进程 kill -STOP 后挂载的表现），走真实的
    __rebuild_watcher -> LocalDirectoryWatcher.start() -> exists() 调用链。

    事故当天冻结的就是这一行（watcher.py 的 start() 入口校验）。修复后看门狗
    必须照常返回，并把该目录转入隔离。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 0.5)
    monitor = _build_monitor(monkeypatch)

    # 用真实 watcher：只把底层线程换成替身，让它被判定为「存活但静默失效」
    watcher = LocalDirectoryWatcher(tmp_path, callback=MagicMock(), force_polling=True)
    watcher._thread = MagicMock()
    watcher._thread.is_alive.return_value = True
    watcher._mark_activity()
    watcher._last_activity -= LocalDirectoryWatcher.STALL_TIMEOUT + 1
    monitor._watchers = [watcher]

    release = threading.Event()
    real_exists = Path.exists

    def blocking_exists(self, *args, **kwargs):
        """
        模拟 block 型挂载：对监控目录的 exists() 永不返回。
        """
        if self == tmp_path:
            release.wait()
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", blocking_exists)
    monkeypatch.setattr("app.monitor.monitor.probe_path", lambda *_a, **_kw: False)

    try:
        assert _run_watchdog(monitor), "看门狗冻死在真实的重建调用链上"
        assert str(tmp_path) in monitor._isolated, "重建无响应后目录没有转入隔离"
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# P0.5：挂载级故障隔离
# --------------------------------------------------------------------------- #

def test_rebuild_timeout_isolates_directory(tmp_path, monkeypatch):
    """
    重建在挂载上超时未返回 = 挂载级故障：该目录转入隔离，后续周期不再对它
    发起任何新的挂载访问，避免每 60 秒泄漏一个冻死的线程。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 0.3)
    monitor = _build_monitor(monkeypatch)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]

    calls = []
    release = threading.Event()

    def blocking_rebuild(target):
        """
        模拟 block 型挂载上的重建：进入后永不返回。
        """
        calls.append(target)
        release.wait()

    setattr(monitor, "_Monitor__rebuild_watcher", blocking_rebuild)
    monkeypatch.setattr("app.monitor.monitor.probe_path", lambda *_a, **_kw: False)

    try:
        assert _run_watchdog(monitor)
        assert str(tmp_path) in monitor._isolated, "重建超时后目录没有转入隔离"

        # 第二个周期：隔离中的目录不得再被提交重建
        assert _run_watchdog(monitor)
        assert len(calls) == 1, "隔离中的目录仍在被反复重建，会持续泄漏冻死的线程"
    finally:
        release.set()


def test_isolated_directory_recovers_after_probe_succeeds(tmp_path, monkeypatch):
    """
    隔离期间用可放弃的子进程探测挂载；探测通过即解除隔离、重建监控，
    并复用既有的补偿扫描补回停摆期间落地的文件。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 5.0)
    monitor = _build_monitor(monkeypatch)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]
    monitor._isolated = {str(tmp_path): {"watcher": watcher, "since": time.time(), "failures": 3}}

    rebuilt = []
    setattr(monitor, "_Monitor__rebuild_watcher", rebuilt.append)
    monkeypatch.setattr("app.monitor.monitor.probe_path", lambda *_a, **_kw: True)

    assert _run_watchdog(monitor)

    assert str(tmp_path) not in monitor._isolated, "探测通过后没有解除隔离"
    assert rebuilt == [watcher], "解除隔离后没有重建监控"


def test_isolated_directory_stays_isolated_while_probe_fails(tmp_path, monkeypatch):
    """
    探测未通过说明挂载仍未恢复应答，必须保持隔离并累计失败次数。
    """
    monkeypatch.setattr(Monitor, "RECOVERY_TIMEOUT", 5.0)
    monitor = _build_monitor(monkeypatch)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]
    monitor._isolated = {str(tmp_path): {"watcher": watcher, "since": time.time(), "failures": 0}}

    rebuilt = []
    setattr(monitor, "_Monitor__rebuild_watcher", rebuilt.append)
    monkeypatch.setattr("app.monitor.monitor.probe_path", lambda *_a, **_kw: False)

    assert _run_watchdog(monitor)

    assert monitor._isolated[str(tmp_path)]["failures"] == 1
    assert rebuilt == [], "挂载尚未恢复就重建监控，只会再冻死一个线程"


def test_pending_locals_skip_isolated_directories(tmp_path, monkeypatch):
    """
    隔离中的目录不能再走启动重试路径，否则又会在同一个挂载上冻死。
    """
    monitor = _build_monitor(monkeypatch)
    monitor._pending_locals = [{"mon_path": tmp_path, "monitor_mode": "compatibility"}]
    monitor._isolated = {str(tmp_path): {"watcher": _fake_watcher(tmp_path), "since": time.time(),
                                         "failures": 0}}
    start = MagicMock(return_value=False)
    setattr(monitor, "_Monitor__start_local_monitor", start)

    monitor._Monitor__retry_pending_locals()

    start.assert_not_called()


def test_isolation_alert_is_pushed_after_fault_alert(tmp_path, monkeypatch):
    """
    故障→隔离是状态升级，必须再推一条告警：沿用「同目录只告警一次」会让
    用户完全看不到「监控已暂停访问、正在等待挂载恢复」这个关键状态变化。
    """
    put_recorder = MagicMock()
    monitor = _build_monitor(monkeypatch, put_recorder)

    monitor._Monitor__send_alert(tmp_path, "目录监控异常")
    monitor._Monitor__send_alert(tmp_path, "目录监控异常")
    assert put_recorder.put.call_count == 1

    monitor._Monitor__send_alert(tmp_path, "挂载无响应，已隔离", stage="isolated")
    assert put_recorder.put.call_count == 2

    monitor._Monitor__clear_alert(tmp_path, "已恢复")
    assert put_recorder.put.call_count == 3
    assert str(tmp_path) not in monitor._alerted_paths


# --------------------------------------------------------------------------- #
# 整理分发器：一个文件卡死不得锁死整条整理链
# --------------------------------------------------------------------------- #

def test_stuck_transfer_does_not_block_other_files(monkeypatch):
    """
    根因回归：整理的规划阶段（do_transfer 里的 get_parent_item / list_files）会
    访问挂载，在 block 型故障下永不返回。若分发器的互斥锁包住这段调用，这把锁
    就会被永久持有，把 13 个 watcher 的事件派发、补偿扫描和重试队列一起锁死
    ——监控层即使自愈成功也送不进任何文件，漏件永远补不回来。

    锁只应保护 TTL 去重的 check-and-set。
    """
    from app.monitor.dispatcher import TransferDispatcher

    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    monkeypatch.setattr(dispatcher, "_should_skip_by_history", MagicMock(return_value=False))

    release = threading.Event()
    stuck_entered = threading.Event()
    transferred = []

    class FakeChain:
        """
        整理链替身：指定文件的整理进入后永不返回。
        """

        @staticmethod
        def do_transfer(fileitem, **_kwargs):
            """
            模拟规划阶段卡在死挂载的 list_files 上。
            """
            transferred.append(fileitem.path)
            if fileitem.path.endswith("stuck.mkv"):
                stuck_entered.set()
                release.wait()

    monkeypatch.setattr("app.monitor.dispatcher.TransferChain", FakeChain)

    def feed(name):
        """
        向分发器送入一个文件。
        """
        dispatcher.handle_file(storage="local", event_path=Path(f"/mnt/cd2/{name}"), file_size=1)

    Thread(target=feed, args=("stuck.mkv",), daemon=True).start()
    assert stuck_entered.wait(timeout=5), "卡死的整理没有真正进入"

    done = threading.Event()
    Thread(target=lambda: (feed("other.mkv"), done.set()), daemon=True).start()

    try:
        assert done.wait(timeout=5), "一个文件卡在死挂载上就锁死了整条整理链"
        assert "/mnt/cd2/other.mkv" in transferred
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# 恢复执行器与挂载探测
# --------------------------------------------------------------------------- #

def test_recovery_executor_reports_timeout_without_blocking():
    """
    永不返回的动作只应消耗一次 timeout，执行器必须放弃它并如实报告。
    """
    executor = RecoveryExecutor()
    release = threading.Event()

    started = time.monotonic()
    results = executor.run({"stuck": release.wait}, timeout=0.3)
    elapsed = time.monotonic() - started

    try:
        assert results == {"stuck": RecoveryState.TIMEOUT}
        assert elapsed < 3.0, "执行器没有在超时后放弃冻死的动作"
    finally:
        release.set()


def test_recovery_executor_skips_key_with_running_task():
    """
    同一个 key 的上一个动作还冻着时，不能再提交新线程——否则每个健康检查
    周期都会在同一个死挂载上泄漏一个线程。
    """
    executor = RecoveryExecutor()
    release = threading.Event()
    calls = []

    def stuck():
        """
        模拟永不返回的恢复动作。
        """
        calls.append(1)
        release.wait()

    try:
        assert executor.run({"k": stuck}, timeout=0.2) == {"k": RecoveryState.TIMEOUT}
        assert executor.run({"k": stuck}, timeout=0.2) == {"k": RecoveryState.BUSY}
        assert len(calls) == 1
    finally:
        release.set()


def test_recovery_executor_runs_actions_concurrently():
    """
    一个周期内多个目录的恢复动作必须并发执行，否则 13 个目录串行等待会把
    健康检查拖过下一个周期。
    """
    executor = RecoveryExecutor()
    release = threading.Event()

    started = time.monotonic()
    results = executor.run({f"k{i}": release.wait for i in range(5)}, timeout=0.4)
    elapsed = time.monotonic() - started

    try:
        assert set(results.values()) == {RecoveryState.TIMEOUT}
        assert elapsed < 1.5, "恢复动作是串行等待的，总耗时随目录数增长"
    finally:
        release.set()


def test_recovery_executor_reports_completion_and_swallows_errors():
    """
    正常完成的动作报告 COMPLETED；动作内部抛异常不能让执行器崩溃，
    否则一个目录的失败会连累整批恢复。
    """
    executor = RecoveryExecutor()
    done = []

    def boom():
        """
        模拟恢复动作内部异常。
        """
        raise RuntimeError("rebuild failed")

    results = executor.run({"ok": lambda: done.append(1), "bad": boom}, timeout=5)

    assert results == {"ok": RecoveryState.COMPLETED, "bad": RecoveryState.COMPLETED}
    assert done == [1]


def test_probe_path_succeeds_on_existing_directory(tmp_path):
    """
    挂载可用时探测应通过。
    """
    assert probe_path(tmp_path, timeout=30) is True


def test_probe_path_fails_on_missing_path(tmp_path):
    """
    路径不存在时探测应失败，而不是抛异常。
    """
    assert probe_path(tmp_path / "does-not-exist", timeout=30) is False


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="需要 POSIX 环境")
def test_probe_path_is_abandonable_on_timeout(tmp_path, monkeypatch):
    """
    探测的关键性质：卡住时可以被放弃。线程做的 stat 无法回收，只有子进程能在
    超时后被 kill——这正是隔离期间能持续探测而不泄漏资源的前提。
    """
    import app.monitor.recovery as recovery

    # 用一个必定超时的探测脚本替换真实探测逻辑，验证超时路径的可放弃性
    monkeypatch.setattr(recovery, "_PROBE_SCRIPT", "import time; time.sleep(60)")

    started = time.monotonic()
    assert probe_path(tmp_path, timeout=0.5) is False
    assert time.monotonic() - started < 10, "探测超时后没有及时放弃子进程"
