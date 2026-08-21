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
