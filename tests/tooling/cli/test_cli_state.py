"""
CLI 状态文件格式和受管进程展示的回归。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from app import cli as cli_module


@pytest.mark.parametrize("payload", [None, [], [1], "invalid", 3, True])
def test_non_object_state_file_is_ignored(tmp_path: Path, payload: object) -> None:
    """
    状态文件即使是合法 JSON，也必须是对象才能交给进程及更新状态消费者。
    """
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli_module._read_json_file(path) is None


def test_object_state_file_preserves_contents(tmp_path: Path) -> None:
    """
    有效状态文件保留原有字段及嵌套内容。
    """
    path = tmp_path / "state.json"
    payload = {"pid": 123, "targets": ["resources"], "metadata": {"ok": True}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli_module._read_json_file(path) == payload


@pytest.mark.parametrize("backend_state", ["running", "starting", "running-unmanaged", "stopped"])
@pytest.mark.parametrize("frontend_state", ["running", "starting", "running-unmanaged", "stopped"])
def test_status_only_reads_managed_process_ids(
    monkeypatch: pytest.MonkeyPatch, backend_state: str, frontend_state: str
) -> None:
    """
    受管服务展示 PID；停止或外部服务没有进程对象时仍可显示状态。
    """
    def service_status(state: str, pid: int) -> tuple:
        """
        返回与服务探测器相同的状态及进程关联。
        """
        if state in {"running", "starting"}:
            return state, {"pid": pid}, SimpleNamespace(pid=pid), {}
        return state, None, None, {}

    monkeypatch.setattr(cli_module, "_managed_backend_status", lambda: service_status(backend_state, 1001))
    monkeypatch.setattr(cli_module, "_managed_frontend_status", lambda: service_status(frontend_state, 2002))
    monkeypatch.setattr(cli_module, "_installed_frontend_version", lambda: "test")
    monkeypatch.setattr(cli_module, "_backend_base_url", lambda *_args: "http://localhost:3001")
    monkeypatch.setattr(cli_module, "_frontend_base_url", lambda *_args: "http://localhost:3000")

    result = CliRunner().invoke(cli_module.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert ("PID: 1001" in result.output) == (backend_state in {"running", "starting"})
    assert ("PID: 2002" in result.output) == (frontend_state in {"running", "starting"})
