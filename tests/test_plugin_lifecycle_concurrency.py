"""插件生命周期入口并发交错时的运行实例归属测试。"""

import threading
from unittest.mock import MagicMock

from app.runtime.extensions.plugin.lifecycle import PluginLifecycle

# 停止一侧在停机 hook 里等待加载一侧的预算。start 不持锁时加载会立刻完成并置位，
# 等待即刻返回；start 持锁时加载被挡在锁外，这里必然等满。两种实现下交错顺序都是
# 确定的，用例不依赖线程被调度的先后。
_INTERLEAVE_BUDGET = 0.3
# 线程握手与回收的宽松预算，仅用于避免实现回归时把整个测试进程挂死
_HANDSHAKE_BUDGET = 5.0


class _SeedInstance:
    """代表上一轮加载留下的运行实例，并在停机 hook 中把执行权让给加载线程。"""

    def __init__(
        self,
        *,
        stopped: list,
        quiesce_entered: threading.Event,
        start_finished: threading.Event,
    ) -> None:
        """记录停机事实，并保存与加载线程握手用的两个事件。"""
        self._stopped = stopped
        self._quiesce_entered = quiesce_entered
        self._start_finished = start_finished

    def stop_service(self) -> None:
        """登记自身已被停止，并在停止事务中途给加载线程留出执行窗口。"""
        self._stopped.append(self)
        self._quiesce_entered.set()
        self._start_finished.wait(_INTERLEAVE_BUDGET)


def _plugin_class(constructed: list) -> type:
    """构造满足最小生命周期合同、并登记自身实例化事实的插件类。"""

    class DemoPlugin:
        """提供并发交错用例需要的最小插件行为。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config) -> None:
            """登记本实例已完成初始化，等价于其后台服务已经拉起。"""
            constructed.append(self)

        @staticmethod
        def get_state() -> bool:
            """并发用例只关心启用态插件，恒定返回启用。"""
            return True

    return DemoPlugin


def _lifecycle(*, plugin_class: type, running: dict) -> PluginLifecycle:
    """构造隔离事件、模块清理和数据库的生命周期实例。"""
    return PluginLifecycle(
        classes={"DemoPlugin": plugin_class},
        running=running,
        load_plugins=lambda _plugin_id, _loadable, _check: [plugin_class],
        loadable_plugins=lambda: ["DemoPlugin"],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: True,
        clear_modules=MagicMock(),
        clear_tools=MagicMock(),
        enable_events=MagicMock(),
        disable_events=MagicMock(),
        runtime_status_writer=lambda _plugin_id, _status: None,
        runtime_compatible=lambda _plugin_id: True,
        database=lambda: MagicMock(),
        log=MagicMock(),
        event_sender=MagicMock(),
    )


def test_concurrent_start_does_not_orphan_the_instance_it_loads() -> None:
    """加载与停止交错时，被加载出来的实例不能既不在运行表也没被停止。

    停止取的是进入时的运行表快照，卸载却按插件 ID 清运行表。start 不与 stop 互斥
    时，交错窗口里加载出来的实例会被这次卸载一并抹掉，而它的 stop_service 从未被
    调用——它注册的定时任务、线程和事件订阅继续在跑，宿主却再也拿不到句柄停它。
    """
    constructed: list = []
    stopped: list = []
    quiesce_entered = threading.Event()
    start_finished = threading.Event()
    seed = _SeedInstance(
        stopped=stopped,
        quiesce_entered=quiesce_entered,
        start_finished=start_finished,
    )
    running: dict = {"DemoPlugin": seed}
    lifecycle = _lifecycle(plugin_class=_plugin_class(constructed), running=running)

    def load() -> None:
        """等停止进入停机 hook 之后再发起加载，制造确定的交错顺序。"""
        assert quiesce_entered.wait(_HANDSHAKE_BUDGET)
        lifecycle.start("DemoPlugin")
        start_finished.set()

    loader = threading.Thread(target=load, name="plugin-start", daemon=True)
    loader.start()
    try:
        lifecycle.stop("DemoPlugin")
    finally:
        loader.join(_HANDSHAKE_BUDGET)

    assert not loader.is_alive()
    assert stopped == [seed]
    assert len(constructed) == 1
    orphaned = [
        instance
        for instance in constructed
        if instance not in running.values() and instance not in stopped
    ]
    assert orphaned == []
