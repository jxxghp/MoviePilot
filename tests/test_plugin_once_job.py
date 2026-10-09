"""插件一次性任务接口测试。

覆盖宿主调度器为插件追加一次性任务的合同：不重建周期服务、同 ID 替换、
取消与卸载清理、执行后回收状态，以及保存配置和重载时对注册实例的处理。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

import app.scheduler.execution as execution_module
import app.scheduler.oncejob as oncejob_module
import app.scheduler.reconcile as reconcile_module
from app.runtime.log import current_plugin_instance_id
from app.scheduler.execution import SchedulerExecutionOwner
from app.scheduler.oncejob import SchedulerPluginOnceJobOwner
from app.scheduler.reconcile import SchedulerReconcileOwner
from app.scheduler.registry import ExecutionRegistry

PID = "DemoOncePlugin"


class _Owner(SchedulerExecutionOwner, SchedulerReconcileOwner, SchedulerPluginOnceJobOwner):
    """按 Scheduler Facade 的组合方式拼装对账与一次性任务两个 owner。"""


class _DemoPlugin:
    """按插件实例绑定方法注册任务的最小插件替身。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.done = threading.Event()

    def run_once(self, **kwargs) -> None:
        self.calls.append({"kwargs": kwargs, "instance_id": current_plugin_instance_id()})
        self.done.set()

    def periodic(self) -> None:
        return None


@pytest.fixture
def owner(monkeypatch):
    """构造使用真实 APScheduler、其余宿主依赖为替身的调度对账 owner。"""
    plugin = _DemoPlugin()
    manager = SimpleNamespace(
        running_plugins={PID: plugin},
        get_plugin_attr=lambda _pid, _attr: "演示插件",
        get_plugin_services=lambda pid: [
            {
                "id": "periodic",
                "name": "周期任务",
                "func": manager.running_plugins[PID].periodic,
                "trigger": "interval",
                "kwargs": {"hours": 1},
            }
        ],
    )
    monkeypatch.setattr(reconcile_module, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(oncejob_module, "get_plugin_manager", lambda: manager)

    scheduler = BackgroundScheduler(timezone="UTC")
    owner = _Owner.__new__(_Owner)
    owner._scheduler = scheduler
    owner._lock = threading.RLock()
    owner._jobs = {}
    owner._assign_job_generation = lambda _job_id, job: job.setdefault("_generation", 0)
    owner._accepting_submissions = lambda: True
    owner._is_job_active = lambda _job_id: False
    owner._registry = ExecutionRegistry(owner._lock)
    owner._lifecycle_state = "running"
    owner._format_time = lambda: "now"
    owner._get_progress_key = lambda job_id: job_id

    def _start(job_id: str) -> bool:
        # 真实 start 的准入、进度与收尾由既有调度测试覆盖，这里只执行已登记的函数。
        job = owner._jobs[job_id]
        if job.get("once"):
            job["_once_pending"] = False
        job["func"](**job["kwargs"])
        return True

    owner.start = _start
    scheduler.start()
    owner.plugin = plugin
    owner.manager = manager
    yield owner
    scheduler.shutdown(wait=False)


def _once_id(job_id: str) -> str:
    return f"{PID}_once_{job_id}"


def test_once_job_runs_with_plugin_context_and_keeps_periodic_jobs(owner) -> None:
    """一次性任务按实例上下文执行，且不重建同插件已注册的周期服务。"""
    owner.update_plugin_job(PID)
    periodic = owner._scheduler.get_job(f"{PID}_periodic")
    next_run = periodic.next_run_time

    assert owner.add_plugin_once_job(PID, "refresh", owner.plugin.run_once, "立即刷新", func_kwargs={"force": True})

    assert owner.plugin.done.wait(5)
    assert owner.plugin.calls == [{"kwargs": {"force": True}, "instance_id": PID}]
    assert owner._scheduler.get_job(f"{PID}_periodic").next_run_time == next_run
    assert owner._jobs[_once_id("refresh")]["provider_name"] == "演示插件"


def test_once_job_with_same_id_replaces_pending_one(owner) -> None:
    """同 ID 重复追加只保留最后一次注册。"""
    assert owner.add_plugin_once_job(PID, "shot", owner.plugin.run_once, "截图", delay_seconds=60, func_kwargs={"n": 1})
    assert owner.add_plugin_once_job(PID, "shot", owner.plugin.run_once, "截图", delay_seconds=60, func_kwargs={"n": 2})

    once_jobs = [job for job in owner._scheduler.get_jobs() if job.id == _once_id("shot")]
    assert len(once_jobs) == 1
    assert owner._jobs[_once_id("shot")]["kwargs"] == {"n": 2}


def test_remove_once_job_and_uninstall_cleanup(owner) -> None:
    """单个取消只影响目标任务，按插件移除时清掉全部一次性任务。"""
    owner.add_plugin_once_job(PID, "a", owner.plugin.run_once, "A", delay_seconds=60)
    owner.add_plugin_once_job(PID, "b", owner.plugin.run_once, "B", delay_seconds=60)

    owner.remove_plugin_once_job(PID, "a")
    assert owner._scheduler.get_job(_once_id("a")) is None
    assert _once_id("a") not in owner._jobs
    assert owner._scheduler.get_job(_once_id("b")) is not None

    owner.remove_plugin_job(PID)
    assert owner._scheduler.get_job(_once_id("b")) is None
    assert not owner._jobs


def test_update_plugin_job_keeps_once_jobs_registered_by_current_instance(owner) -> None:
    """保存配置时 init_plugin 先追加任务、宿主后重建服务，任务必须保留。"""
    owner.add_plugin_once_job(PID, "onlyonce", owner.plugin.run_once, "立即运行一次", delay_seconds=60)

    owner.update_plugin_job(PID)

    assert owner._scheduler.get_job(_once_id("onlyonce")) is not None
    assert _once_id("onlyonce") in owner._jobs
    assert owner._scheduler.get_job(f"{PID}_periodic") is not None


def test_update_plugin_job_keeps_once_job_after_scheduler_dispatch(owner) -> None:
    """APScheduler 已消费触发器但尚未进入执行准入时，服务重建仍保留一次性任务。"""
    job_id = _once_id("dispatching")
    assert owner.add_plugin_once_job(
        PID,
        "dispatching",
        owner.plugin.run_once,
        "正在派发的一次性任务",
        delay_seconds=60,
    )

    # DateTrigger 到期后 APScheduler 会先从 job store 移除任务，再调用 start。
    owner._scheduler.remove_job(job_id)
    owner.update_plugin_job(PID)

    assert job_id in owner._jobs
    assert owner.start(job_id)
    assert owner.plugin.done.wait(5)
    assert len(owner.plugin.calls) == 1


def test_prepare_job_clears_once_pending_state_after_admission(owner, monkeypatch) -> None:
    """一次性任务进入执行准入后不再以待派发状态阻止后续回收。"""

    class _ProgressStub:
        """隔离一次性任务准入测试的进度缓存。"""

        def __init__(self, _key: str) -> None:
            """接收进度键但不访问缓存后端。"""

        def start(self) -> None:
            """记录进度开始。"""

        def update(self, **_kwargs) -> None:
            """忽略本用例不关心的进度快照。"""

    monkeypatch.setattr(execution_module, "ProgressHelper", _ProgressStub)
    job_id = _once_id("admitted")
    assert owner.add_plugin_once_job(
        PID,
        "admitted",
        owner.plugin.run_once,
        "已准入的一次性任务",
        delay_seconds=60,
    )

    job = owner._prepare_job(job_id)

    assert job is owner._jobs[job_id]
    assert job["_once_pending"] is False


def test_update_plugin_job_drops_once_jobs_of_replaced_instance(owner) -> None:
    """插件重载换成新实例后，旧实例追加的任务不得再回调旧实例。"""
    owner.add_plugin_once_job(PID, "stale", owner.plugin.run_once, "旧实例任务", delay_seconds=60)
    owner.manager.running_plugins[PID] = _DemoPlugin()

    owner.update_plugin_job(PID)

    assert owner._scheduler.get_job(_once_id("stale")) is None
    assert _once_id("stale") not in owner._jobs


def test_finished_once_job_state_is_pruned_on_next_registration(owner) -> None:
    """执行完的一次性任务状态在下次追加时回收，不随任务 ID 累积。"""
    owner.add_plugin_once_job(PID, "first", owner.plugin.run_once, "第一次")
    assert owner.plugin.done.wait(5)

    owner.add_plugin_once_job(PID, "second", owner.plugin.run_once, "第二次", delay_seconds=60)

    assert _once_id("first") not in owner._jobs
    assert _once_id("second") in owner._jobs


def test_once_job_provider_falls_back_to_plugin_id(owner) -> None:
    """插件名称读不到时用插件 ID 作为提供方，避免显示 None。"""
    owner.manager.get_plugin_attr = lambda _pid, _attr: None

    assert owner.add_plugin_once_job(PID, "noname", owner.plugin.run_once, "无名称", delay_seconds=60)

    assert owner._jobs[_once_id("noname")]["provider_name"] == PID


def test_once_job_rejected_when_scheduler_not_accepting_or_id_invalid(owner) -> None:
    """调度器不再接受提交或任务 ID 非法时返回 False，且不留下状态。"""
    assert not owner.add_plugin_once_job(PID, "bad|id", owner.plugin.run_once, "非法")
    owner._accepting_submissions = lambda: False
    assert not owner.add_plugin_once_job(PID, "late", owner.plugin.run_once, "停机中")
    assert not owner._jobs
