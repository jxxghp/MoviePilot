"""工作流运行时 Application Facade 回归。"""

import pytest

from app.application import workflow as workflow_application


def test_workflow_runtime_facade_preserves_registered_identity(monkeypatch) -> None:
    """Application Facade 必须返回组合根登记的同一个运行时对象。"""
    runtime = object()
    monkeypatch.setattr(
        workflow_application,
        "_workflow_runtime_provider",
        lambda: runtime,
    )

    assert workflow_application.get_workflow_manager() is runtime


def test_workflow_runtime_facade_fails_before_composition(monkeypatch) -> None:
    """未装配时不得隐式创建第二个 WorkFlowManager Singleton。"""
    monkeypatch.setattr(
        workflow_application,
        "_workflow_runtime_provider",
        workflow_application._unconfigured_workflow_runtime,
    )

    with pytest.raises(RuntimeError, match="工作流运行时尚未由启动组合根装配"):
        workflow_application.get_workflow_manager()
