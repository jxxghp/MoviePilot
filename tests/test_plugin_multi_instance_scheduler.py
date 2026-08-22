"""插件多实例定时服务隔离的行为契约测试。

覆盖同一插件的多个实例声明同名服务 id 时任务互不覆盖、按实例键精确移除
单个实例的服务且不误删兄弟实例、按插件标识移除时命中该插件的全部实例。
"""

import threading
from unittest.mock import Mock

from apscheduler.jobstores.base import JobLookupError

from app.runtime import log as log_module
from app.scheduler import Scheduler


class _FakeJob:
    """记录一次 add_job 调用产生的任务身份。"""

    def __init__(self, job_id: str) -> None:
        """保存任务 id。"""
        self.id = job_id


class _FakeAPScheduler:
    """只记录任务 id 集合的调度器替身，不真正调度执行。"""

    def __init__(self) -> None:
        """初始化空的任务 id 登记表。"""
        self._job_ids: list[str] = []

    def add_job(self, func, trigger=None, *, id, **kwargs):
        """记录一次任务注册，同 id 视为覆盖登记。"""
        if id not in self._job_ids:
            self._job_ids.append(id)

    def remove_job(self, job_id):
        """按 id 精确移除任务，未登记时抛出 JobLookupError。"""
        if job_id not in self._job_ids:
            raise JobLookupError(job_id)
        self._job_ids.remove(job_id)

    def get_jobs(self):
        """返回当前登记任务的快照。"""
        return [_FakeJob(job_id) for job_id in self._job_ids]


def _build_scheduler() -> Scheduler:
    """构造不启动真实 APScheduler 的调度器测试对象。"""
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = _FakeAPScheduler()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    return scheduler


def _fake_service(service_id: str, pid: str) -> dict:
    """构造带归属实例键的插件服务声明，模拟投影层补齐后的结构。"""
    return {
        "id": service_id,
        "name": f"服务-{service_id}",
        "trigger": "interval",
        "func": lambda **_kwargs: None,
        "pid": pid,
    }


def _patch_plugin_manager(monkeypatch, services: list[dict]) -> None:
    """把调度器使用的插件管理器替换为返回固定服务清单的替身。"""
    plugin_manager = Mock()
    plugin_manager.get_plugin_services.return_value = services
    plugin_manager.get_plugin_attr.return_value = "Demo 插件"
    monkeypatch.setattr("app.scheduler.plugins.PluginManager", lambda: plugin_manager)


def test_two_instances_register_same_service_id_without_overwriting(monkeypatch):
    """两个实例声明同名服务 id 时，各自的任务都被登记，互不覆盖。"""
    scheduler = _build_scheduler()
    _patch_plugin_manager(
        monkeypatch,
        [
            _fake_service("sync", "DemoPlugin"),
            _fake_service("sync", "DemoPlugin@second"),
        ],
    )

    scheduler.update_plugin_job("DemoPlugin")

    assert set(scheduler._jobs) == {"DemoPlugin_sync", "DemoPlugin@second_sync"}
    assert scheduler._jobs["DemoPlugin_sync"]["pid"] == "DemoPlugin"
    assert scheduler._jobs["DemoPlugin@second_sync"]["pid"] == "DemoPlugin@second"
    assert set(scheduler._scheduler._job_ids) == {
        "DemoPlugin_sync",
        "DemoPlugin@second_sync",
    }


def test_removing_one_instance_keeps_sibling_service(monkeypatch):
    """删一个实例的服务时按实例键精确命中，不误删兄弟实例的服务。"""
    scheduler = _build_scheduler()
    _patch_plugin_manager(
        monkeypatch,
        [
            _fake_service("sync", "DemoPlugin"),
            _fake_service("sync", "DemoPlugin@second"),
        ],
    )
    scheduler.update_plugin_job("DemoPlugin")

    scheduler.remove_plugin_job("DemoPlugin@second")

    assert set(scheduler._jobs) == {"DemoPlugin_sync"}
    assert set(scheduler._scheduler._job_ids) == {"DemoPlugin_sync"}


def test_removing_whole_plugin_removes_every_instance_service(monkeypatch):
    """按插件标识移除时命中该插件全部实例的服务。"""
    scheduler = _build_scheduler()
    _patch_plugin_manager(
        monkeypatch,
        [
            _fake_service("sync", "DemoPlugin"),
            _fake_service("sync", "DemoPlugin@second"),
        ],
    )
    scheduler.update_plugin_job("DemoPlugin")

    scheduler.remove_plugin_job("DemoPlugin")

    assert scheduler._jobs == {}
    assert scheduler._scheduler._job_ids == []


def test_registered_job_func_binds_plugin_instance_log_context(monkeypatch):
    """注册到调度器的任务回调触发时应绑定其归属实例的日志上下文。

    定时任务的实际调用发生在宿主稍后触发的调度线程/事件循环里，这里验证
    `update_plugin_job` 在登记阶段把 `service["func"]` 包上的上下文绑定确实生效：
    执行期间 ContextVar 能读到正确的 (插件标识, 实例标识)，执行完毕后复原。
    """
    scheduler = _build_scheduler()
    observed = {}

    def default_instance_probe(**_kwargs):
        observed["default"] = log_module.LoggerManager._resolve_plugin_instance(None)

    def second_instance_probe(**_kwargs):
        observed["second"] = log_module.LoggerManager._resolve_plugin_instance(None)

    services = [
        {
            "id": "sync",
            "name": "服务-sync",
            "trigger": "interval",
            "func": default_instance_probe,
            "pid": "DemoPlugin",
        },
        {
            "id": "sync",
            "name": "服务-sync",
            "trigger": "interval",
            "func": second_instance_probe,
            "pid": "DemoPlugin@second",
        },
    ]
    _patch_plugin_manager(monkeypatch, services)

    scheduler.update_plugin_job("DemoPlugin")
    scheduler._jobs["DemoPlugin_sync"]["func"]()
    scheduler._jobs["DemoPlugin@second_sync"]["func"]()

    assert observed["default"] == ("DemoPlugin", "default")
    assert observed["second"] == ("DemoPlugin", "second")
    # 任务执行完毕后绑定应已复原，不残留到后续无关调用
    assert log_module.LoggerManager._resolve_plugin_instance(None) == (None, None)
