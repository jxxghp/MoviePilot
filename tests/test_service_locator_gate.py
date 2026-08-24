"""进程级运行时 Facade 静态门禁回归测试。"""

from scripts.architecture.service_locator import (
    collect_service_locator_violations,
)


def test_host_runtime_consumers_use_application_facades() -> None:
    """canonical 宿主不得在批准边界外直接定位 concrete 运行时。"""
    assert collect_service_locator_violations() == []


def test_service_locator_gate_detects_each_concrete_runtime_family() -> None:
    """单一扫描器必须覆盖五类运行时，并保留明确兼容边界。"""
    graph = {
        "app.api.scheduler_bypass": {"app.scheduler"},
        "app.api.module_bypass": {"app.runtime.extensions.module_manager"},
        "app.api.plugin_bypass": {"app.runtime.extensions.plugin_manager"},
        "app.api.command_bypass": {"app.command"},
        "app.api.workflow_bypass": {"app.workflow"},
        "app.sdk.plugins": {
            "app.runtime.extensions.module_manager",
            "app.runtime.extensions.plugin_manager",
        },
        "app.startup.initializers.modules": {
            "app.command",
            "app.runtime.extensions.module_manager",
            "app.runtime.extensions.plugin_manager",
            "app.scheduler",
        },
        "app.workflow.manager": {"app.workflow"},
    }

    violations = collect_service_locator_violations(graph)

    assert [violation.policy for violation in violations] == [
        "command",
        "module",
        "plugin",
        "scheduler",
        "workflow",
    ]
    assert all(violation.source.startswith("app.api.") for violation in violations)
