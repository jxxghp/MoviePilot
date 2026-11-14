"""
验证工作流恢复、并发缓存、调度和共享引用锁的审查回归。
"""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.application.configuration import SystemConfigService
from app.chain import workflow as workflow_module
from app.monitor.monitor import Monitor
from app.monitor.poller import RemotePoller
from app.monitor.snapshot import SnapshotStore
from app.runtime.cache import MemoryBackend
from app.scheduler.reconcile import SchedulerReconcileOwner
from app.startup.composition import subscription as composition
from app.workflow.actions import BaseAction
from tests.test_workflow_execution import _build_workflow, _FakeWorkflowManager
from tests.test_workflow_mutation_command import _command, _workflow


@pytest.mark.asyncio
async def test_cancelled_reference_lock_waiter_releases_late_acquisition(monkeypatch):
    """
    被取消请求的工作线程迟到拿锁后仍应释放，后续请求可以继续修改。
    """
    lock = threading.Lock()
    attempted = threading.Event()
    acquired = threading.Event()

    class ObservedLock:
        """
        用事件观察真实线程的锁获取，不访问数据库。
        """

        def acquire(self):
            """
            报告开始等待和成功获取，以固定取消时序。
            """
            attempted.set()
            result = lock.acquire()
            acquired.set()
            return result

        def release(self):
            """
            释放真实锁。
            """
            lock.release()

    lock.acquire()
    monkeypatch.setattr(composition, "_reference_mutation_lock", ObservedLock())

    async def wait_for_scope():
        """
        只进入引用修改作用域，取消后不得实际创建数据库会话。
        """
        async with composition.async_rule_group_mutation_scope(lambda _values: None):
            raise AssertionError("取消后不应进入事务")

    task = asyncio.create_task(wait_for_scope())
    assert await asyncio.to_thread(attempted.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock.release()
    assert await asyncio.to_thread(acquired.wait, 5)
    await asyncio.sleep(0)
    available = await asyncio.to_thread(lock.acquire, True, 1)
    if available:
        lock.release()
    else:
        lock.release()
    assert available


def test_failed_workflow_node_retries_on_resume(monkeypatch):
    """
    恢复时重新执行失败动作，并重新计算原先因失败跳过的下游。
    """
    calls = []
    workflow = _build_workflow(execution_state={"nodes": {"A": {"state": "failed"}, "B": {"state": "skipped"}}})
    monkeypatch.setattr(workflow_module, "get_workflow_manager", lambda: _FakeWorkflowManager(calls))
    executor = workflow_module.WorkflowExecutor(workflow)
    executor.execute()
    assert calls == ["A", "B"]
    assert executor.success


def test_append_unique_respects_nested_identity():
    """
    身份相同的不同内容只保留第一条，身份不同的条目继续追加。
    """
    executor = workflow_module.WorkflowExecutor.__new__(workflow_module.WorkflowExecutor)
    first = {"media": {"id": "123"}, "name": "初始"}
    second = {"media": {"id": "123"}, "name": "修改"}
    third = {"media": {"id": "456"}, "name": "其他"}
    assert executor.append_unique_values([first], [second, third], "media.id") == [first, third]


@pytest.mark.parametrize("context", [{"content": "合法文本"}, {"content": "合法文本", "workflow_context": {"key": "value"}}])
def test_current_text_context_survives_resume(context, monkeypatch):
    """
    当前结构的文本内容不是旧序列化载荷，不得丢失恢复数据。
    """
    monkeypatch.setattr(workflow_module, "get_workflow_manager", lambda: _FakeWorkflowManager([]))
    executor = workflow_module.WorkflowExecutor(_build_workflow(context=context))
    try:
        assert executor.context.content == "合法文本"
        assert executor.context.workflow_context == context.get("workflow_context", {})
    finally:
        executor.executor.shutdown_bounded(timeout=1)


def test_paused_workflow_edit_does_not_register_timer():
    """
    修改暂停定义仍可保存，但不得重新启动定时执行。
    """
    workflow = _workflow()
    workflow.state = "P"
    command, dependencies = _command(workflow)
    assert command.update({"id": 7, "description": "修改说明"}).success
    dependencies["add_timer"].assert_not_called()


def test_monitor_interval_change_uses_real_scheduler(monkeypatch):
    """
    文件数增长后真实 APScheduler 的下轮触发间隔随之调整。
    """
    monitor = Monitor.__new__(Monitor)
    monitor._scheduler = BackgroundScheduler()
    monitor._scheduler.add_job(lambda: None, IntervalTrigger(minutes=1), id="monitor_remote")
    monitor._poller = SimpleNamespace(poll=lambda **_kwargs: 1001)
    monkeypatch.setattr(monitor, "_Monitor__accepting_work", lambda: True)
    monitor.polling_observer("remote", [Path("/library")])
    assert monitor._scheduler.get_job("monitor_remote").trigger.interval.total_seconds() == (
        SnapshotStore.adjust_interval(1001) * 60
    )


def test_workflow_cron_uses_scheduler_timezone():
    """
    Cron 触发器必须沿用调度器时区，不能隐式使用宿主时区。
    """
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    owner = Mock(_scheduler=scheduler, _lock=threading.RLock(), _jobs={})
    owner.start = lambda **_kwargs: None
    workflow = SimpleNamespace(id=7, name="时区", timer="0 8 * * *")
    SchedulerReconcileOwner.update_workflow_job(owner, workflow)
    assert str(scheduler.get_job("workflow-7").trigger.timezone) == "Asia/Shanghai"


@pytest.mark.parametrize("existing_baseline", [False, True])
def test_single_root_full_scan_preserves_shared_incremental_cursor(monkeypatch, existing_baseline):
    """
    只扫描一个根不应让另一根尚未消费的修改被共享游标越过。
    """
    store = SnapshotStore(cache=MemoryBackend())
    if existing_baseline:
        assert store.save("remote", {"/b/old.mkv": {"size": 1, "modify_time": 10}}, snapshot_time=10)
    dispatcher = SimpleNamespace(is_transfer_candidate_path=lambda _path: True, handle_file=lambda **_kwargs: True)
    poller = RemotePoller(store, dispatcher)
    monkeypatch.setattr("app.monitor.poller.StorageChain", lambda: SimpleNamespace(
        snapshot_storage=lambda **_kwargs: {"/a/new.mkv": {"size": 1, "modify_time": 100}},
    ))
    assert poller.force_full_scan("remote", Path("/a"))
    loaded, success = store.load_checked("remote")
    assert success
    assert loaded["timestamp"] == (10 if existing_baseline else 0)
    if existing_baseline:
        assert "/b/old.mkv" in loaded["snapshot"]


def test_parallel_action_cache_updates_are_atomic():
    """
    并行动作通过同一持久原子更新入口合并，不能丢失已成功分支。
    """
    state = {}
    lock = threading.Lock()
    rendezvous = threading.Barrier(2)

    class Repository:
        """
        在真实线程中回放配置事务，分离读取会固定得到同一旧值。
        """

        def get(self, key):
            """
            模拟两个分离读取同时读到旧快照。
            """
            value = copy.deepcopy(state.get(key))
            rendezvous.wait(timeout=5)
            return value

        def set(self, key, value):
            """
            保存配置快照。
            """
            state[key] = copy.deepcopy(value)

        def update_atomically(self, key, mutation):
            """
            在同一锁内读取并提交一次配置变更。
            """
            with lock:
                result, value = mutation(None, copy.deepcopy(state.get(key)))
                state[key] = value
                return result

    service = SystemConfigService(reader=Repository(), writer=Repository())
    actions = []
    for action_id in ["A", "B"]:
        action = Mock(_action_id=action_id, _cache_key="WorkflowCache_%s", systemconfigoper=service)
        actions.append(action)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda action: BaseAction.save_cache(action, 7, action._action_id), actions))
    assert state["WorkflowCache_7"] == {"A": ["A"], "B": ["B"]}
