"""
验证插件候选初始化失败及停止失败时的资源所有权。
"""

import threading
from unittest.mock import Mock

import pytest

from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus
from tests.test_plugin_database_lifecycle import _build_lifecycle


@pytest.mark.parametrize("stage", ["init", "database", "state"])
def test_failed_candidate_closes_started_thread_before_releasing_database(stage):
    """
    初始化、建库或状态查询失败时仍收尾已启动线程，数据库只在收敛后释放。
    """
    stopped = threading.Event()
    created = []
    released = []

    class Candidate:
        """
        初始化启动真实线程并按参数触发后续故障的隔离插件。
        """
        plugin_name = "candidate"
        plugin_version = "1"

        def __init__(self):
            """
            记录唯一实例并创建尚未启动的线程。
            """
            created.append(self)
            self.thread = threading.Thread(target=stopped.wait)

        def init_plugin(self, _config):
            """
            模拟初始化已产生资源后发生异常。
            """
            self.thread.start()
            if stage == "init":
                raise RuntimeError("init failed")

        def get_state(self):
            """
            模拟初始化之后的状态投影失败。
            """
            if stage == "state":
                raise RuntimeError("state failed")
            return True

        def close(self):
            """
            关闭并等待本实例持有的生产者线程。
            """
            stopped.set()
            self.thread.join(5)

    def ensure(_plugin_id, _models, _migrations):
        """
        在需要时模拟插件自有数据库初始化失败。
        """
        if stage == "database":
            raise RuntimeError("database failed")

    def release(plugin_id):
        """
        数据库关闭不得先于依赖数据库的生产者停止。
        """
        released.append((plugin_id, created[0].thread.is_alive()))

    running = {}
    lifecycle = _build_lifecycle(
        running=running, load_plugins=lambda *_: [Candidate],
        database=lambda: PluginDatabase(ensure=ensure, release=release),
    )
    try:
        assert lifecycle.start("Candidate") == {"Candidate": PluginRuntimeStatus.LOAD_FAILED}
        assert not created[0].thread.is_alive()
        assert released == [("Candidate", False)]
        assert not running
    finally:
        stopped.set()
        created[0].thread.join(5)


def test_failed_candidate_retains_owner_and_blocks_duplicate_initialization():
    """
    候选关闭失败时保留所有权，重试启动不能覆盖旧实例，随后可再次停止。
    """
    created = []
    allow_close = [False]
    release = Mock()

    class Candidate:
        """
        初始化失败且第一次关闭不收敛的插件。
        """
        plugin_name = "candidate"
        plugin_version = "1"

        def __init__(self):
            """
            记录每次构造以检测资源owner被重复创建。
            """
            created.append(self)

        def init_plugin(self, _config):
            """
            在生命周期已经持有实例时触发初始化失败。
            """
            raise RuntimeError("init failed")

        def close(self):
            """
            允许后续显式停止重试收敛。
            """
            return allow_close[0]

    running = {}
    lifecycle = _build_lifecycle(
        running=running, load_plugins=lambda *_: [Candidate],
        database=lambda: PluginDatabase(release=release),
    )
    assert lifecycle.start("Candidate")["Candidate"] is PluginRuntimeStatus.LOAD_FAILED
    assert running["Candidate"] is created[0]
    release.assert_not_called()
    assert lifecycle.start("Candidate")["Candidate"] is PluginRuntimeStatus.LOAD_FAILED
    assert len(created) == 1
    allow_close[0] = True
    assert lifecycle.stop("Candidate") is not False
    assert not running
    release.assert_called_once_with("Candidate")


@pytest.mark.parametrize("hook", ["close", "stop_service"])
def test_stop_and_reload_preserve_nonconverged_owner(hook):
    """
    停止hook未收敛时不丢弃owner，不释放数据库，也不允许reload创建新实例。
    """
    plugin = Mock()
    plugin.close.return_value = None
    plugin.stop_service.return_value = None
    getattr(plugin, hook).return_value = False
    running = {"Demo": plugin}
    classes = {"Demo": type(plugin)}
    loader, clear_modules, release, events = Mock(return_value=[]), Mock(), Mock(), Mock()
    statuses = {}
    lifecycle = _build_lifecycle(
        running=running, classes=classes, load_plugins=loader,
        clear_modules=clear_modules, event_sender=events,
        runtime_status_writer=statuses.__setitem__,
        database=lambda: PluginDatabase(release=release),
    )
    assert lifecycle.stop("Demo") is False
    assert running["Demo"] is plugin
    assert statuses["Demo"] is PluginRuntimeStatus.LOAD_FAILED
    assert lifecycle.reload("Demo", "reloaded") is PluginRuntimeStatus.LOAD_FAILED
    loader.assert_not_called()
    clear_modules.assert_not_called()
    release.assert_not_called()
    events.assert_not_called()
    getattr(plugin, hook).return_value = None
    assert lifecycle.stop("Demo") is not False
    assert not running
    release.assert_called_once_with("Demo")


def test_manager_remove_keeps_registry_when_lifecycle_stop_fails():
    """
    管理器兼容移除入口必须传播停止失败，不能再次从共享注册表丢弃活owner。
    """
    manager = object.__new__(PluginManager)
    from contextlib import nullcontext
    manager.mutation = lambda _: nullcontext()
    manager._plugin_quiesce_lock = threading.RLock()
    manager._plugin_lifecycle = Mock()
    manager._plugin_lifecycle.stop.return_value = False
    manager._plugin_registry = Mock()
    assert manager.remove_plugin("Demo") is False
    manager._plugin_registry.remove.assert_not_called()
