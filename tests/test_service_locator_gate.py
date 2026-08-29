"""进程级运行时 Facade 与 provider 装配门禁回归测试。"""

import subprocess
import sys
from pathlib import Path

from scripts.architecture.service_locator import (
    collect_service_locator_violations,
)


def test_host_runtime_consumers_use_application_facades() -> None:
    """canonical 宿主不得在批准边界外直接定位 concrete 运行时。"""
    assert collect_service_locator_violations() == []


def test_service_locator_gate_detects_each_concrete_runtime_family() -> None:
    """单一扫描器必须覆盖五类运行时，并保留明确兼容边界。"""
    graph = {
        "app.api.scheduler_bypass": {"app.scheduler.facade"},
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
            "app.scheduler.facade",
        },
        "app.startup.composition.chain": {
            "app.runtime.extensions.module_manager",
            "app.runtime.extensions.plugin_manager",
        },
        "app.startup.composition.outbox": {"app.command"},
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


def test_initializer_provider_registration_requires_explicit_stage() -> None:
    """冷导入 initializer 不得注册 provider；configure/reset 必须成对生效。"""
    root = Path(__file__).resolve().parents[1]
    script = r'''
from app.testing.bootstrap import ensure_sites_stub

ensure_sites_stub()

import app.application.agent as agent
import app.application.commands as commands
import app.application.messaging.skill as skill
import app.application.scheduling as scheduling
import app.application.workflow as workflow
import app.agent.llm.gateway as gateway

from app.startup.initializers import agent as agent_init
from app.startup.initializers import command as command_init
from app.startup.initializers import scheduler as scheduler_init
from app.startup.initializers import workflow as workflow_init

assert commands._command_class is None
assert scheduling._scheduler_class is None
assert workflow._workflow_runtime_provider is workflow._unconfigured_workflow_runtime
assert agent._agent_manager_provider is None
assert skill._skill_catalog_provider is None
assert gateway._provider_runtime_factory is None

command_init.configure_command_runtime()
scheduler_init.configure_scheduler_runtime()
workflow_init.configure_workflow_ports()
agent_init.configure_agent_ports()
assert commands._command_class is not None
assert scheduling._scheduler_class is not None
assert workflow._workflow_runtime_provider is not workflow._unconfigured_workflow_runtime
assert agent._agent_manager_provider is not None
assert skill._skill_catalog_provider is not None
assert gateway._provider_runtime_factory is not None

command_init.reset_command_runtime()
scheduler_init.reset_scheduler_runtime()
workflow_init.reset_workflow_ports()
agent_init.reset_agent_ports()
assert commands._command_class is None
assert scheduling._scheduler_class is None
assert workflow._workflow_runtime_provider is workflow._unconfigured_workflow_runtime
assert agent._agent_manager_provider is None
assert skill._skill_catalog_provider is None
assert gateway._provider_runtime_factory is None
'''

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
