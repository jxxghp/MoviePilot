"""
本地进程状态记录及跨平台子进程参数的兼容回归。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import cli
from app.runtime.state import SystemHelper


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [(None, True), (100, True), (102.0, True), (103, False), ("100", True), ("invalid", False), ([], False), ({}, False)],
)
def test_runtime_identity_checks_creation_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, created_at: object, expected: bool
) -> None:
    """
    创建时间可缺省或使用数字字符串；损坏记录不能冒充当前受管进程。
    """
    state_path = tmp_path / "runtime.json"
    state_path.write_text(json.dumps({"pid": os.getpid(), "create_time": created_at}), encoding="utf-8")
    monkeypatch.setattr(SystemHelper, "_SystemHelper__local_backend_runtime_file", state_path)
    monkeypatch.setattr("app.runtime.state.psutil.Process", lambda _pid: SimpleNamespace(create_time=lambda: 100.0))

    assert SystemHelper._is_local_cli_managed() is expected


@pytest.mark.parametrize("pid", [None, [], {}, "invalid", 0, float("inf"), float("-inf"), float("nan")])
def test_invalid_process_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pid: object
) -> None:
    """
    不可转换的 PID 和空 PID 均不建立受管进程身份。
    """
    state_path = tmp_path / "runtime.json"
    state_path.write_text(json.dumps({"pid": pid}), encoding="utf-8")
    monkeypatch.setattr(SystemHelper, "_SystemHelper__local_backend_runtime_file", state_path)

    assert SystemHelper._is_local_cli_managed() is False


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("entrypoint", ["cli", "restart"])
def test_detached_process_flags_match_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, entrypoint: str
) -> None:
    """
    模拟平台并截获启动参数，验证 Windows 分离标志和 Unix 新会话保持一致。
    """
    for attribute, name in (
        ("_SystemHelper__local_restart_log_file", "restart.log"),
        ("_SystemHelper__prepared_update_manifest", "install.json"),
        ("_SystemHelper__one_shot_dev_update_flag_file", "pending-update"),
    ):
        monkeypatch.setattr(SystemHelper, attribute, tmp_path / name)
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(sys, "platform", platform)
        if entrypoint == "cli":
            cli._spawn_process(["test-command"], cwd=tmp_path, log_file=None)
        else:
            SystemHelper._spawn_local_restart_helper()

    spawn.assert_called_once()
    options = spawn.call_args.kwargs
    assert options["stdin"] == subprocess.DEVNULL
    assert options["close_fds"] is True
    if platform == "win32":
        assert options["creationflags"] == 0x208
        assert "start_new_session" not in options
    else:
        assert options["start_new_session"] is True
        assert "creationflags" not in options
