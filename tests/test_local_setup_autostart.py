from __future__ import annotations

import importlib.util
import subprocess
import uuid
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """加载独立模块实例，避免测试间共享本地安装脚本状态。"""
    module_name = f"moviepilot_local_setup_autostart_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_linux_systemd_unit_keeps_services_alive_and_retries_startup_failures(
    monkeypatch, tmp_path: Path
) -> None:
    """systemd unit 应覆盖 CLI 启动窗口，并在成功后保留派生服务。"""
    module = load_local_setup_module()
    launcher = tmp_path / "moviepilot-start.sh"
    launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    unit_path = tmp_path / module.LINUX_SYSTEMD_UNIT_NAME
    desktop_path = tmp_path / module.LINUX_XDG_AUTOSTART_FILENAME
    command_calls: list[list[str]] = []

    def run_optional_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        command_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(module, "_write_unix_startup_launcher", lambda **kwargs: launcher)
    monkeypatch.setattr(module, "_linux_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(module, "_linux_xdg_autostart_path", lambda: desktop_path)
    monkeypatch.setattr(module, "_run_optional_command", run_optional_command)
    monkeypatch.setattr(module, "_linux_linger_enabled", lambda: True)
    monkeypatch.setattr(module, "write_env_value", lambda *args: None)

    result = module._enable_autostart_linux_systemd(
        config_dir=tmp_path / "config",
        python_bin=tmp_path / "venv" / "bin" / "python",
    )

    unit_content = unit_path.read_text(encoding="utf-8")
    assert "Type=oneshot" in unit_content
    assert "RemainAfterExit=yes" in unit_content
    assert "TimeoutStartSec=infinity" in unit_content
    assert "Restart=on-failure" in unit_content
    assert f"RestartSec={module.LINUX_SYSTEMD_RESTART_DELAY}" in unit_content
    assert command_calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", module.LINUX_SYSTEMD_UNIT_NAME],
        ["/usr/bin/systemctl", "--user", "start", module.LINUX_SYSTEMD_UNIT_NAME],
    ]
    assert result and result["method"] == "systemd --user"


def test_disabling_linux_autostart_stops_active_unit_before_removing_it(
    monkeypatch, tmp_path: Path
) -> None:
    """取消自启动必须停止 active (exited) unit 及其 cgroup 子进程。"""
    module = load_local_setup_module()
    unit_path = tmp_path / module.LINUX_SYSTEMD_UNIT_NAME
    desktop_path = tmp_path / module.LINUX_XDG_AUTOSTART_FILENAME
    launcher_path = tmp_path / "moviepilot-start.sh"
    unit_path.write_text("[Service]\nRemainAfterExit=yes\n", encoding="utf-8")
    desktop_path.write_text("[Desktop Entry]\n", encoding="utf-8")
    launcher_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    command_calls: list[list[str]] = []

    def run_optional_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        command_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(module, "_linux_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(module, "_linux_xdg_autostart_path", lambda: desktop_path)
    monkeypatch.setattr(module, "AUTOSTART_UNIX_LAUNCHER", launcher_path)
    monkeypatch.setattr(module, "_run_optional_command", run_optional_command)
    monkeypatch.setattr(module, "write_env_value", lambda *args: None)

    result = module.disable_autostart()

    assert command_calls == [
        [
            "/usr/bin/systemctl",
            "--user",
            "disable",
            "--now",
            module.LINUX_SYSTEMD_UNIT_NAME,
        ],
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
    ]
    assert set(result["removed_paths"]) == {unit_path, desktop_path}
    assert not unit_path.exists()
    assert not desktop_path.exists()
    assert not launcher_path.exists()
