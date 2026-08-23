"""插件生命周期六类状态中的运行结果测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.schemas.plugin import PluginRuntimeStatus


def _plugin_class(*, init_error: Exception | None = None):
    """构造满足插件最小生命周期合同的测试类。"""
    class DemoPlugin:
        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            if init_error:
                raise init_error

        @staticmethod
        def get_state():
            return True

    return DemoPlugin


def _lifecycle(*, plugins, auth=True):
    """构造隔离外部事件和模块清理的生命周期实例。"""
    classes = {}
    running = {}
    statuses = {}
    lifecycle = PluginLifecycle(
        classes=classes,
        running=running,
        load_plugins=lambda _plugin_id, _installed, _check: list(plugins),
        installed_plugins=lambda: ["DemoPlugin"],
        plugin_config=lambda _plugin_id: {},
        auth_checker=lambda _plugin: auth,
        clear_modules=MagicMock(),
        clear_tools=MagicMock(),
        enable_events=MagicMock(),
        disable_events=MagicMock(),
        runtime_status_writer=statuses.__setitem__,
        log=MagicMock(),
        event_sender=MagicMock(),
    )
    return lifecycle, classes, running, statuses


def test_lifecycle_records_active_result():
    """插件完成构造和初始化后进入 active。"""
    lifecycle, classes, running, statuses = _lifecycle(
        plugins=[_plugin_class()],
    )

    result = lifecycle.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.ACTIVE}
    assert "DemoPlugin" in classes
    assert "DemoPlugin" in running
    assert statuses["DemoPlugin"] is PluginRuntimeStatus.ACTIVE


def test_lifecycle_records_policy_block_without_runtime_instance():
    """类已发现但权限策略拒绝时进入 blocked_by_policy。"""
    lifecycle, _classes, running, statuses = _lifecycle(
        plugins=[_plugin_class()],
        auth=False,
    )

    result = lifecycle.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.BLOCKED_BY_POLICY}
    assert running == {}
    assert statuses["DemoPlugin"] is PluginRuntimeStatus.BLOCKED_BY_POLICY


def test_lifecycle_records_load_failure_for_init_exception():
    """插件初始化异常时保留类信息并进入 load_failed。"""
    lifecycle, classes, running, statuses = _lifecycle(
        plugins=[_plugin_class(init_error=RuntimeError("init failed"))],
    )

    result = lifecycle.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert "DemoPlugin" in classes
    assert running == {}
    assert statuses["DemoPlugin"] is PluginRuntimeStatus.LOAD_FAILED


def test_lifecycle_records_load_failure_when_loader_returns_no_class():
    """目标源码无法产生合法插件类时进入 load_failed。"""
    lifecycle, _classes, running, statuses = _lifecycle(plugins=[])

    result = lifecycle.start("DemoPlugin")

    assert result == {"DemoPlugin": PluginRuntimeStatus.LOAD_FAILED}
    assert running == {}
    assert statuses["DemoPlugin"] is PluginRuntimeStatus.LOAD_FAILED


def test_quiesce_keeps_instance_and_events_until_finalize():
    """第一阶段仅停止插件生产者，事件 handler 和实例留到屏障后释放。"""
    order: list[str] = []

    class DemoPlugin:
        """记录旧插件 ABI hook 调用顺序的测试插件。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            """接受宿主初始化配置。"""

        @staticmethod
        def get_state():
            """保持插件事件 handler 启用。"""
            return True

        def close(self):
            """停止插件私有计时器和 watcher。"""
            order.append("close")

        def stop_service(self):
            """停止插件服务并允许尾事件继续投递。"""
            order.append("stop_service")

    lifecycle, classes, running, _statuses = _lifecycle(plugins=[DemoPlugin])
    lifecycle.start("DemoPlugin")
    lifecycle._disable_events.reset_mock()
    lifecycle._clear_modules.reset_mock()
    lifecycle._clear_tools.reset_mock()

    assert lifecycle.quiesce() is True
    assert order == ["close", "stop_service"]
    assert classes["DemoPlugin"] is DemoPlugin
    assert isinstance(running["DemoPlugin"], DemoPlugin)
    lifecycle._disable_events.assert_not_called()
    lifecycle._clear_modules.assert_not_called()
    lifecycle._clear_tools.assert_not_called()

    assert lifecycle.finalize() is True
    lifecycle._disable_events.assert_called_once_with(DemoPlugin)
    lifecycle._clear_modules.assert_called_once_with(None)
    lifecycle._clear_tools.assert_called_once_with()
    assert classes == {}
    assert running == {}


def test_quiesce_runs_stop_service_after_close_failure_and_retries_missing_hook():
    """close 异常不能跳过 stop_service，重试时只补偿尚未成功的 hook。"""
    order: list[str] = []
    close_fails = [True]

    class DemoPlugin:
        """首次 close 失败、stop_service 正常完成的测试插件。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            """接受宿主初始化配置。"""

        @staticmethod
        def get_state():
            """保持插件为启用状态。"""
            return True

        def close(self):
            """首次调用模拟插件私有资源关闭异常。"""
            order.append("close")
            if close_fails[0]:
                raise RuntimeError("close failed")

        def stop_service(self):
            """记录旧 ABI 的第二个停机 hook。"""
            order.append("stop_service")

    lifecycle, classes, running, _statuses = _lifecycle(plugins=[DemoPlugin])
    lifecycle.start("DemoPlugin")

    assert lifecycle.quiesce() is False
    assert order == ["close", "stop_service"]
    assert lifecycle.finalize() is False
    assert "DemoPlugin" in classes
    assert "DemoPlugin" in running

    close_fails[0] = False
    assert lifecycle.quiesce() is True
    assert order == ["close", "stop_service", "close"]
    assert lifecycle.finalize() is True
    assert classes == {}
    assert running == {}


def test_legacy_stop_entry_remains_idempotent():
    """旧 stop 先解绑事件、保持 None 返回，且重复调用不重复执行 hook。"""
    order: list[str] = []

    class DemoPlugin:
        """提供可计数停机 hook 的兼容测试插件。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            """接受宿主初始化配置。"""

        @staticmethod
        def get_state():
            """保持插件为启用状态。"""
            return True

        def close(self):
            """记录 close 调用。"""
            order.append("close")

        def stop_service(self):
            """记录 stop_service 调用。"""
            order.append("stop_service")

    lifecycle, _classes, running, _statuses = _lifecycle(plugins=[DemoPlugin])
    lifecycle.start("DemoPlugin")
    lifecycle._disable_events.reset_mock()
    lifecycle._disable_events.side_effect = (
        lambda _plugin_type: order.append("disable_events")
    )

    assert lifecycle.stop() is None
    assert lifecycle.stop() is None
    assert order == ["disable_events", "close", "stop_service"]
    lifecycle._disable_events.assert_called_once_with(DemoPlugin)
    assert running == {}


def test_legacy_stop_force_finalizes_after_hook_failure():
    """旧 stop 即使 hook 报错也释放实例，并保持历史 None 返回 ABI。"""
    order: list[str] = []

    class DemoPlugin:
        """close 失败但仍应完成兼容卸载的测试插件。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def init_plugin(self, _config):
            """接受宿主初始化配置。"""

        @staticmethod
        def get_state():
            """保持插件为启用状态。"""
            return True

        def close(self):
            """模拟旧插件关闭异常。"""
            order.append("close")
            raise RuntimeError("close failed")

        def stop_service(self):
            """证明 close 异常不会跳过后续 hook。"""
            order.append("stop_service")

    lifecycle, classes, running, _statuses = _lifecycle(plugins=[DemoPlugin])
    lifecycle.start("DemoPlugin")
    lifecycle._disable_events.reset_mock()
    lifecycle._clear_modules.reset_mock()

    assert lifecycle.stop("DemoPlugin") is None
    assert order == ["close", "stop_service"]
    lifecycle._disable_events.assert_called_once_with(DemoPlugin)
    lifecycle._clear_modules.assert_called_once_with("DemoPlugin")
    assert classes == {}
    assert running == {}


def test_legacy_reload_continues_after_old_instance_hook_failure():
    """旧实例 hook 失败只影响诊断，不得阻止 reload 创建新实例。"""
    instances: list[object] = []
    stop_service = MagicMock()

    class DemoPlugin:
        """记录每次构造并让 close 持续失败的重载测试插件。"""

        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def __init__(self):
            """记录新建的运行实例。"""
            instances.append(self)

        def init_plugin(self, _config):
            """接受宿主初始化配置。"""

        @staticmethod
        def get_state():
            """保持插件为启用状态。"""
            return True

        @staticmethod
        def close():
            """模拟旧实例退出异常。"""
            raise RuntimeError("close failed")

        def stop_service(self):
            """记录旧 ABI 的第二个停机 hook。"""
            stop_service()

    lifecycle, _classes, running, statuses = _lifecycle(plugins=[DemoPlugin])
    lifecycle.start("DemoPlugin")
    previous = running["DemoPlugin"]

    result = lifecycle.reload("DemoPlugin", "plugin-reload")

    assert result is PluginRuntimeStatus.ACTIVE
    assert statuses["DemoPlugin"] is PluginRuntimeStatus.ACTIVE
    assert running["DemoPlugin"] is not previous
    assert len(instances) == 2
    stop_service.assert_called_once_with()
    lifecycle._event_sender.assert_called_once_with(
        "plugin-reload",
        data={"plugin_id": "DemoPlugin"},
    )
