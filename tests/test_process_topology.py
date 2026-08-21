"""V3 API 数据面与宿主控制面进程拓扑测试。"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.doctor import checks
from app.doctor.models import DoctorFindingStatus
from app.doctor.runner import DoctorRunner
from app.runtime.config import ConfigModel, settings
from app.runtime.topology import (
    UnsupportedProcessTopologyError,
    process_topology_issue,
    validate_process_topology,
)
from app.startup import lifecycle


@pytest.mark.parametrize("safe_mode", [False, True])
def test_single_worker_is_supported_in_every_mode(safe_mode: bool):
    """单 worker 是正常模式和安全模式共同支持的默认拓扑。"""
    assert process_topology_issue(workers=1, safe_mode=safe_mode) is None
    validate_process_topology(workers=1, safe_mode=safe_mode)


def test_full_runtime_rejects_multiple_workers():
    """全功能模式不得复制插件、调度器、监控器和工作流。"""
    with pytest.raises(
        UnsupportedProcessTopologyError,
        match="全功能模式仅支持 API_WORKERS=1",
    ):
        validate_process_topology(workers=2, safe_mode=False)


def test_safe_mode_temporarily_allows_multiple_workers():
    """安全模式跳过控制面时允许多 worker 作为故障诊断手段。"""
    assert process_topology_issue(workers=2, safe_mode=True) is None
    validate_process_topology(workers=2, safe_mode=True)


@pytest.mark.parametrize("workers", [0, -1, "invalid"])
def test_api_workers_configuration_rejects_invalid_values(workers):
    """worker 数量的边界或类型错误必须在配置解析阶段暴露。"""
    with pytest.raises(ValidationError):
        ConfigModel(API_WORKERS=workers)


def test_main_rejects_topology_before_startup_side_effects(monkeypatch):
    """主入口应在注册信号、迁移数据库和启动服务器前拒绝错误拓扑。"""
    from app import main

    monkeypatch.setattr(main.settings, "API_WORKERS", 2)
    monkeypatch.setattr(main.settings, "MOVIEPILOT_SAFE_MODE", False)
    signal_handler = MagicMock()
    start_tray = MagicMock()
    prepare_database = MagicMock()
    server_run = MagicMock()
    monkeypatch.setattr(main.signal, "signal", signal_handler)
    monkeypatch.setattr(main, "start_tray", start_tray)
    monkeypatch.setattr(main, "prepare_database", prepare_database)
    monkeypatch.setattr(main.Server, "run", server_run)

    with pytest.raises(UnsupportedProcessTopologyError):
        main.run_application()

    signal_handler.assert_not_called()
    start_tray.assert_not_called()
    prepare_database.assert_not_called()
    server_run.assert_not_called()


def test_asgi_lifespan_rejects_topology_before_runtime_initialization(monkeypatch):
    """外部 ASGI supervisor 也必须在生命周期副作用前执行同一校验。"""
    monkeypatch.setattr(settings, "API_WORKERS", 2)
    monkeypatch.setattr(settings, "MOVIEPILOT_SAFE_MODE", False)
    set_loop = MagicMock()
    monkeypatch.setattr(lifecycle.global_vars, "set_loop", set_loop)

    async def run_lifespan() -> None:
        async with lifecycle.lifespan(FastAPI()):
            pass

    with pytest.raises(UnsupportedProcessTopologyError):
        asyncio.run(run_lifespan())

    set_loop.assert_not_called()


def test_doctor_fails_unsupported_full_runtime_topology(monkeypatch):
    """Doctor 应把正常模式多 worker 报告为影响整体状态的失败。"""
    monkeypatch.setattr(settings, "API_WORKERS", 2)
    monkeypatch.setattr(settings, "MOVIEPILOT_SAFE_MODE", False)
    runner = DoctorRunner()

    checks._check_process_topology(runner)

    finding = runner.report.find("startup.process_topology")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Failed
    assert finding.context == {"api_workers": 2, "safe_mode": False}


def test_doctor_marks_safe_mode_multi_worker_as_degraded(monkeypatch):
    """安全模式多 worker 可运行，但 Doctor 必须提醒它不是正式扩容方案。"""
    monkeypatch.setattr(settings, "API_WORKERS", 2)
    monkeypatch.setattr(settings, "MOVIEPILOT_SAFE_MODE", True)
    runner = DoctorRunner()

    checks._check_process_topology(runner)

    finding = runner.report.find("startup.process_topology")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Degraded
