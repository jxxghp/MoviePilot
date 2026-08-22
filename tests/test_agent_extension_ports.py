"""agent 扩展经端口取用自检诊断与工作流执行能力的行为验证。

覆盖端口未注册时的报错、组合根注册后端口解析到可用实现，
以及 doctor 诊断报告工具、工作流执行工具经端口取用宿主服务的集成路径。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.impl.run_workflow import RunWorkflowTool
from app.runtime.config import settings
from app.runtime.hostports.diagnostics import diagnostics_port
from app.runtime.hostports.workflows import workflow_execution_port
from app.startup.hostport_initializer import configure_host_ports


@pytest.fixture(autouse=True)
def _restore_agent_extension_ports():
    """用例结束后恢复组合根注册的实现，避免影响其它用例。"""
    yield
    configure_host_ports()


def test_diagnostics_port_raises_clear_error_when_not_registered():
    """自检诊断端口未注册时应给出可定位的报错。"""
    diagnostics_port.reset()
    with pytest.raises(RuntimeError, match="diagnostics"):
        diagnostics_port.resolve()


def test_workflow_execution_port_raises_clear_error_when_not_registered():
    """工作流执行端口未注册时应给出可定位的报错。"""
    workflow_execution_port.reset()
    with pytest.raises(RuntimeError, match="workflow_execution"):
        workflow_execution_port.resolve()


def test_configure_host_ports_registers_working_diagnostics(tmp_path, monkeypatch):
    """组合根注册后自检诊断端口应解析到 doctor 扩展提供的实现。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    settings.LOG_PATH.mkdir(parents=True, exist_ok=True)
    (settings.ROOT_PATH / "public").mkdir(exist_ok=True)
    configure_host_ports()

    diagnostics = diagnostics_port.resolve()

    report = diagnostics.run_doctor(deep=False)
    assert hasattr(report, "to_dict")
    assert isinstance(report.to_dict(), dict)


class _FakeWorkflowExecutionProvider:
    """工作流执行工具集成测试用的工作流执行替身。"""

    def __init__(self, state: bool, errmsg: str = ""):
        self._state = state
        self._errmsg = errmsg
        self.calls: list[tuple[int, bool]] = []

    def process(self, workflow_id: int, from_begin=True):
        self.calls.append((workflow_id, from_begin))
        return self._state, self._errmsg


def test_configure_host_ports_registers_working_workflow_execution():
    """组合根注册后工作流执行端口应解析到 workflow 扩展提供的实现。"""
    configure_host_ports()

    workflow_execution = workflow_execution_port.resolve()

    assert hasattr(workflow_execution, "process")


def test_run_workflow_tool_consumes_registered_workflow_execution_port():
    """工作流执行工具应经端口取用注册的工作流执行实现。"""
    tool = RunWorkflowTool(session_id="workflow-session", user_id="10001")
    workflow = SimpleNamespace(id=1, name="demo-workflow")
    workflow_oper = MagicMock()
    workflow_oper.async_get = AsyncMock(return_value=workflow)
    provider = _FakeWorkflowExecutionProvider(state=True)
    workflow_execution_port.register(lambda: provider)

    with patch(
        "app.agent.tools.impl.run_workflow.WorkflowOper",
        return_value=workflow_oper,
    ):
        result = asyncio.run(tool.run(workflow_id=1, from_begin=True))

    assert "执行成功" in result
    assert provider.calls == [(1, True)]


def test_run_workflow_tool_reports_error_from_registered_provider():
    """工作流执行工具应把端口实现返回的失败原因透传给调用方。"""
    tool = RunWorkflowTool(session_id="workflow-session", user_id="10001")
    workflow = SimpleNamespace(id=2, name="demo-workflow")
    workflow_oper = MagicMock()
    workflow_oper.async_get = AsyncMock(return_value=workflow)
    provider = _FakeWorkflowExecutionProvider(state=False, errmsg="动作执行失败")
    workflow_execution_port.register(lambda: provider)

    with patch(
        "app.agent.tools.impl.run_workflow.WorkflowOper",
        return_value=workflow_oper,
    ):
        result = asyncio.run(tool.run(workflow_id=2, from_begin=False))

    assert "执行工作流失败" in result
    assert "动作执行失败" in result
    assert provider.calls == [(2, False)]
