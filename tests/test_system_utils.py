import asyncio
import errno
import itertools
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import psutil
import pytest

from app.adapters.system.host import SystemUtils
from app.runtime.config import ConfigModel, Settings
from app.runtime.state import SystemHelper


def test_get_config_path_uses_repository_config_for_source_runtime():
    """源码运行时应从项目根目录读取配置，不能随适配器目录层级偏移。"""
    expected = Path(__file__).resolve().parents[1] / "config"

    with patch.dict(os.environ, {}, clear=True), \
            patch.object(SystemUtils, "is_docker", return_value=False), \
            patch.object(SystemUtils, "is_frozen", return_value=False):
        assert SystemUtils.get_config_path() == expected
        assert SystemUtils.get_env_path() == expected / "app.env"


def test_get_config_path_preserves_explicit_config_dir():
    """显式配置目录始终优先于运行环境推导。"""
    explicit_path = Path("/custom/moviepilot-config")

    with patch.dict(os.environ, {"CONFIG_DIR": "/ignored"}, clear=True), \
            patch.object(SystemUtils, "is_docker", return_value=True):
        assert SystemUtils.get_config_path(str(explicit_path)) == explicit_path


def test_get_config_path_preserves_runtime_specific_defaults():
    """容器和冻结程序继续使用各自稳定的配置目录。"""
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(SystemUtils, "is_docker", return_value=True):
        assert SystemUtils.get_config_path() == Path("/config")

    with patch.dict(os.environ, {}, clear=True), \
            patch.object(SystemUtils, "is_docker", return_value=False), \
            patch.object(SystemUtils, "is_frozen", return_value=True), \
            patch("app.adapters.system.host.sys.executable", "/opt/moviepilot/moviepilot"):
        assert SystemUtils.get_config_path() == Path("/opt/moviepilot/config")


def test_execute_with_subprocess_keeps_stdout_when_command_fails():
    """命令失败时如果原因只写入 stdout，也需要回传给调用方用于错误提示。"""
    error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pip", "check"],
        output="demo requires pkg>=2, but you have pkg 1\n",
        stderr="",
    )

    with patch("app.adapters.system.host.subprocess.run", side_effect=error):
        success, message = SystemUtils.execute_with_subprocess(["pip", "check"])

    assert not success
    assert "返回码：1" in message
    assert "标准输出：demo requires pkg>=2, but you have pkg 1" in message


def test_execute_with_subprocess_reports_empty_failure_output():
    """命令失败且没有输出时应给出明确占位信息，避免错误原因看起来被截断。"""
    error = subprocess.CalledProcessError(
        returncode=2,
        cmd=["pip", "check"],
        output="",
        stderr="",
    )

    with patch("app.adapters.system.host.subprocess.run", side_effect=error):
        success, message = SystemUtils.execute_with_subprocess(["pip", "check"])

    assert not success
    assert "返回码：2" in message
    assert "无标准输出或错误输出" in message


def test_docker_restart_delegates_to_supervisor():
    """容器内重启只委托 supervisor，不向当前进程或 Docker daemon 发信号。"""
    with patch("app.runtime.state.is_docker", return_value=True), \
            patch.object(SystemHelper, "_SystemHelper__supervisor_config") as supervisor_config, \
            patch.object(SystemHelper, "_SystemHelper__supervisorctl") as supervisorctl, \
            patch.object(SystemHelper, "_SystemHelper__supervisor_socket") as supervisor_socket, \
            patch.object(SystemHelper, "_SystemHelper__prepared_update_manifest") as prepared_manifest, \
            patch.object(SystemHelper, "_SystemHelper__one_shot_dev_update_flag_file") as dev_update_flag, \
            patch.object(SystemHelper, "_schedule_supervisor_restart") as restart_mock, \
            patch.object(SystemHelper, "_schedule_supervisor_shutdown") as shutdown_mock, \
            patch("app.runtime.state.os.kill") as kill_mock:
        supervisor_config.exists.return_value = True
        supervisorctl.exists.return_value = True
        supervisor_socket.exists.return_value = True
        prepared_manifest.is_file.return_value = False
        dev_update_flag.is_file.return_value = False
        ret, msg = SystemHelper.restart()

    assert ret
    assert msg == ""
    restart_mock.assert_called_once_with()
    shutdown_mock.assert_not_called()
    kill_mock.assert_not_called()


def test_docker_update_restart_reenters_entrypoint_for_pending_install():
    """待安装更新先启动 root worker 替换 Docker 程序目录。"""
    with patch("app.runtime.state.is_docker", return_value=True), \
            patch.object(SystemHelper, "_SystemHelper__supervisor_config") as supervisor_config, \
            patch.object(SystemHelper, "_SystemHelper__supervisorctl") as supervisorctl, \
            patch.object(SystemHelper, "_SystemHelper__supervisor_socket") as supervisor_socket, \
            patch.object(SystemHelper, "_SystemHelper__prepared_update_manifest") as prepared_manifest, \
            patch.object(SystemHelper, "_SystemHelper__one_shot_dev_update_flag_file") as dev_update_flag, \
            patch.object(SystemHelper, "_schedule_supervisor_restart") as restart_mock, \
            patch.object(SystemHelper, "_schedule_supervisor_shutdown") as shutdown_mock, \
            patch.object(SystemHelper, "_schedule_supervisor_command") as command_mock:
        supervisor_config.exists.return_value = True
        supervisorctl.exists.return_value = True
        supervisor_socket.exists.return_value = True
        prepared_manifest.is_file.return_value = True
        dev_update_flag.is_file.return_value = False
        ret, msg = SystemHelper.restart()

    assert ret
    assert msg == ""
    command_mock.assert_called_once_with("start", "moviepilot-update-worker")
    restart_mock.assert_not_called()
    shutdown_mock.assert_not_called()


def test_supervisor_restart_command_restarts_frontend_and_backend(monkeypatch):
    """延迟任务必须通过本地 supervisor 同时重启前后端进程。"""
    callback = None

    class ImmediateTimer:
        def __init__(self, _delay, timer_callback):
            nonlocal callback
            callback = timer_callback
            self.daemon = False

        def start(self):
            callback()

    popen_mock = MagicMock()
    monkeypatch.setattr("app.runtime.state.threading.Timer", ImmediateTimer)
    monkeypatch.setattr("app.runtime.state.subprocess.Popen", popen_mock)

    SystemHelper._schedule_supervisor_restart()

    assert popen_mock.call_args.args[0][-2:] == ["restart", "all"]


def test_supervisor_shutdown_command(monkeypatch):
    """一次性 Dev 更新使用 supervisor shutdown，交回 root 入口执行更新流程。"""
    callback = None

    class ImmediateTimer:
        def __init__(self, _delay, timer_callback):
            nonlocal callback
            callback = timer_callback
            self.daemon = False

        def start(self):
            callback()

    popen_mock = MagicMock()
    monkeypatch.setattr("app.runtime.state.threading.Timer", ImmediateTimer)
    monkeypatch.setattr("app.runtime.state.subprocess.Popen", popen_mock)

    SystemHelper._schedule_supervisor_shutdown()

    assert popen_mock.call_args.args[0][-1:] == ["shutdown"]


def test_upgrade_dev_always_marks_bootstrap_update():
    """Dev 更新即使已配置 dev 模式也要留下入口消费标记。"""
    with patch.object(SystemHelper, "queue_one_shot_dev_update", return_value=(True, "")) as queue_mock, \
            patch.object(SystemHelper, "restart", return_value=(True, "")) as restart_mock:
        ret, msg = SystemHelper.upgrade_dev()

    assert ret
    assert msg == "已安排 Dev 更新并重启"
    queue_mock.assert_called_once_with()
    restart_mock.assert_called_once_with()


def test_execute_with_subprocess_passes_env_to_subprocess():
    with patch("app.adapters.system.host.subprocess.run") as run_mock:
        run_mock.return_value.stdout = "ok"
        run_mock.return_value.stderr = ""

        success, message = SystemUtils.execute_with_subprocess(
            ["pip", "check"],
            env={"PIP_CACHE_DIR": "/config/.cache/pip"},
        )

    assert success
    assert message == "ok"
    assert run_mock.call_args.kwargs["env"]["PIP_CACHE_DIR"] == "/config/.cache/pip"


def test_execute_with_subprocess_uses_safe_command_in_failure_message():
    error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pip", "install", "-i", "https://user:pass@mirror.example/simple"],
        output="",
        stderr="failed",
    )

    command = ["pip", "install", "-i", "https://user:pass@mirror.example/simple"]
    with patch("app.adapters.system.host.subprocess.run", side_effect=error) as run_mock:
        success, message = SystemUtils.execute_with_subprocess(
            command,
            safe_command=["pip", "install", "-i", "https://mirror.example/simple"],
        )

    assert not success
    assert "https://mirror.example/simple" in message
    assert "user:pass" not in message
    assert run_mock.call_args.args[0] == command


@pytest.mark.asyncio
async def test_async_subprocess_timeout_reaps_process():
    """异步安装命令超时后应终止并回收子进程。"""
    success, message = await SystemUtils.execute_with_subprocess_async(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout=0.05,
    )

    assert success is False
    assert "执行超时" in message


@pytest.mark.asyncio
async def test_async_subprocess_cancellation_reaps_process(tmp_path):
    """调用方取消安装任务时，底层子进程不得继续运行。"""
    marker = tmp_path / "pid"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import os, time; "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
        ),
    ]
    task = asyncio.create_task(
        SystemUtils.execute_with_subprocess_async(command, timeout=30)
    )
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert marker.exists()

    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"子进程仍在运行：{pid}")


@pytest.mark.asyncio
async def test_async_subprocess_cancellation_reaps_process_tree(tmp_path):
    """取消安装命令时，子进程派生的构建进程也不得继续运行。"""
    marker = tmp_path / "pids"
    child_code = "import time; time.sleep(60)"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import subprocess, os, time; "
            f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
            f"Path({str(marker)!r}).write_text(str(os.getpid()) + ':' + str(child.pid)); "
            "time.sleep(60)"
        ),
    ]
    task = asyncio.create_task(
        SystemUtils.execute_with_subprocess_async(command, timeout=30)
    )
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert marker.exists()

    pids = [int(value) for value in marker.read_text().split(":")]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        alive = []
        for pid in pids:
            try:
                process = psutil.Process(pid)
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    continue
            except (psutil.Error, OSError):
                continue
            alive.append(pid)
        if not alive:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"进程树仍在运行：{alive}")


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有 POSIX 进程组信号语义")
@pytest.mark.asyncio
async def test_async_subprocess_reaps_descendant_after_early_pipe_close(tmp_path):
    """父进程关闭管道后，忽略终止信号的后代也必须被强制回收。"""
    marker = tmp_path / "pids"
    child_code = (
        "import os, signal, time; os.close(1); os.close(2); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import os, signal, subprocess, time; "
            f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}], "
            "start_new_session=True); "
            f"Path({str(marker)!r}).write_text(str(os.getpid()) + ':' + str(child.pid)); "
            "signal.signal(signal.SIGTERM, lambda *_: os._exit(0)); time.sleep(60)"
        ),
    ]
    task = asyncio.create_task(
        SystemUtils.execute_with_subprocess_async(command, timeout=30)
    )
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert marker.exists()

    pids = [int(value) for value in marker.read_text().split(":")]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        alive = []
        for pid in pids:
            try:
                process = psutil.Process(pid)
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    continue
            except (psutil.Error, OSError):
                continue
            alive.append(pid)
        if not alive:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"通信已结束但进程树仍在运行：{alive}")


def test_execute_with_subprocess_redacts_userinfo_from_stdout_and_stderr():
    error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pip", "install"],
        output="Looking in indexes: https://user:pass@mirror.example/simple",
        stderr="Proxy failed: http://proxy_user:proxy_pass@proxy.example:7890",
    )

    with patch("app.adapters.system.host.subprocess.run", side_effect=error):
        success, message = SystemUtils.execute_with_subprocess(["pip", "install"])

    assert not success
    assert "https://mirror.example/simple" in message
    assert "http://proxy.example:7890" in message
    assert "user:pass" not in message
    assert "proxy_user:proxy_pass" not in message


def test_execute_with_subprocess_redacts_userinfo_from_non_http_scheme():
    error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pip", "install"],
        output="Proxy failed: socks5://proxy_user:proxy_pass@proxy.example:7890",
        stderr="Resolved direct URL: git+https://git_user:git_pass@example.com/org/repo.git",
    )

    with patch("app.adapters.system.host.subprocess.run", side_effect=error):
        success, message = SystemUtils.execute_with_subprocess(["pip", "install"])

    assert not success
    assert "socks5://proxy.example:7890" in message
    assert "git+https://example.com/org/repo.git" in message
    assert "proxy_user:proxy_pass" not in message
    assert "git_user:git_pass" not in message


def test_execute_with_subprocess_redacts_success_output_userinfo():
    with patch("app.adapters.system.host.subprocess.run") as run_mock:
        run_mock.return_value.stdout = "Using https://user:pass@mirror.example/simple\n"
        run_mock.return_value.stderr = "Proxy socks5://proxy_user:proxy_pass@proxy.example:7890\n"

        success, message = SystemUtils.execute_with_subprocess(["pip", "install"])

    assert success
    assert "https://mirror.example/simple" in message
    assert "socks5://proxy.example:7890" in message
    assert "user:pass" not in message
    assert "proxy_user:proxy_pass" not in message


def test_execute_with_subprocess_redacts_unknown_error_userinfo_and_invalid_port():
    with patch(
        "app.adapters.system.host.subprocess.run",
        side_effect=RuntimeError("bad url https://user:pass@example.com:notaport/simple"),
    ):
        success, message = SystemUtils.execute_with_subprocess(["pip", "install"])

    assert not success
    assert "https://example.com:notaport/simple" in message
    assert "user:pass" not in message


def _fake_stat_with_devs(dev_by_path):
    """构造按路径返回指定 st_dev 的 os.stat 桩。"""
    def _fake_stat(path, *args, **kwargs):
        result = MagicMock()
        result.st_dev = dev_by_path[str(path)]
        return result
    return _fake_stat


def _fill_fs_info(fsid, num_devices=1, truncate=False):
    """构造填充 BTRFS_IOC_FS_INFO 缓冲区的 ioctl 桩。"""
    def _ioctl(fd, request, buffer, mutate_flag):
        assert fd == 42
        assert request == 0x8400941F
        assert mutate_flag is True
        if truncate:
            del buffer[64:]
            return 0
        struct.pack_into("=Q", buffer, 8, num_devices)
        buffer[16:32] = fsid
        return 0
    return _ioctl


@pytest.fixture
def linux_platform(monkeypatch):
    """将当前用例的平台标识切换为 Linux amd64。"""
    monkeypatch.setattr("app.adapters.system.host.sys.platform", "linux")
    monkeypatch.setattr(SystemUtils, "is_x86_64", lambda: True)
    monkeypatch.setattr(SystemUtils, "is_aarch64", lambda: False)


@pytest.mark.parametrize("num_devices", [1, 2])
def test_get_btrfs_fsid_reads_kernel_result_and_closes_fd(num_devices, linux_platform):
    fsid = bytes.fromhex("88e3aff5fa2946d591a55977be984655")
    with patch("app.adapters.system.host.os.open", return_value=42), \
            patch("app.adapters.system.host.fcntl.ioctl", side_effect=_fill_fs_info(fsid, num_devices)), \
            patch("app.adapters.system.host.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result == fsid
    close_mock.assert_called_once_with(42)


@pytest.mark.parametrize("error_number", [errno.ENOTTY, errno.EACCES, errno.EPERM, errno.EINVAL])
def test_get_btrfs_fsid_falls_back_on_expected_ioctl_errors(error_number, linux_platform):
    with patch("app.adapters.system.host.os.open", return_value=42), \
            patch("app.adapters.system.host.fcntl.ioctl", side_effect=OSError(error_number, os.strerror(error_number))), \
            patch("app.adapters.system.host.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    close_mock.assert_called_once_with(42)


def test_get_btrfs_fsid_falls_back_when_directory_cannot_be_opened(linux_platform):
    with patch("app.adapters.system.host.os.open", side_effect=OSError(errno.EACCES, "denied")), \
            patch("app.adapters.system.host.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    close_mock.assert_not_called()


@pytest.mark.parametrize(
    ("fsid", "num_devices", "truncate"),
    [
        (bytes(16), 1, False),
        (b"valid-fsid-value", 0, False),
        (b"valid-fsid-value", 1, True),
    ],
)
def test_get_btrfs_fsid_rejects_invalid_kernel_results(fsid, num_devices, truncate, linux_platform):
    with patch("app.adapters.system.host.os.open", return_value=42), \
            patch("app.adapters.system.host.fcntl.ioctl", side_effect=_fill_fs_info(fsid, num_devices, truncate)), \
            patch("app.adapters.system.host.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    close_mock.assert_called_once_with(42)


def test_get_btrfs_fsid_is_disabled_outside_linux():
    with patch("app.adapters.system.host.sys.platform", "darwin"), \
            patch("app.adapters.system.host.os.open") as open_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    open_mock.assert_not_called()


def test_get_btrfs_fsid_is_disabled_on_unsupported_linux_architecture():
    with patch("app.adapters.system.host.sys.platform", "linux"), \
            patch.object(SystemUtils, "is_x86_64", return_value=False), \
            patch.object(SystemUtils, "is_aarch64", return_value=False), \
            patch("app.adapters.system.host.os.open") as open_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    open_mock.assert_not_called()


def test_btrfs_fsid_dedup_setting_is_opt_in():
    assert ConfigModel().BTRFS_FSID_DEDUP is False
    assert ConfigModel(BTRFS_FSID_DEDUP="true").BTRFS_FSID_DEDUP is True


@pytest.mark.parametrize("mode", ["release", "dev", " DEV ", "RELEASE"])
def test_auto_update_mode_is_normalized(monkeypatch, mode):
    """旧模式规范化为布尔 true，已有的新 Dev 偏好不被覆盖。"""
    updates = []
    monkeypatch.setattr(
        Settings,
        "update_env_config",
        lambda field, original, converted: updates.append(
            (field, original, converted)
        ),
    )

    config = Settings(MOVIEPILOT_AUTO_UPDATE=mode, MOVIEPILOT_UPDATE_DEV=False)
    assert config.MOVIEPILOT_AUTO_UPDATE is True
    assert config.MOVIEPILOT_UPDATE_DEV is False
    assert updates == [
        ("MOVIEPILOT_AUTO_UPDATE", mode, True),
    ]


def test_legacy_dev_tracking_is_migrated_once(monkeypatch, tmp_path):
    """首次拆分配置时持久化两个开关，重读后继续保留 Dev 跟踪。"""
    env_file = tmp_path / "app.env"
    env_file.write_text("MOVIEPILOT_AUTO_UPDATE='dev'\n", encoding="utf-8")
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: env_file)
    config = Settings(_env_file=env_file)
    assert config.MOVIEPILOT_AUTO_UPDATE is True
    assert config.MOVIEPILOT_UPDATE_DEV is True
    assert "MOVIEPILOT_AUTO_UPDATE='true'" in env_file.read_text(encoding="utf-8")
    assert "MOVIEPILOT_UPDATE_DEV='true'" in env_file.read_text(encoding="utf-8")
    reloaded = Settings(_env_file=env_file)
    assert reloaded.MOVIEPILOT_AUTO_UPDATE is True
    assert reloaded.MOVIEPILOT_UPDATE_DEV is True


@pytest.mark.parametrize("enabled", [True, False, "true", "false"])
def test_update_switches_remain_independent_booleans(enabled):
    """部署设置与调度快照仅暴露布尔值，Dev 跟踪不影响自动检查。"""
    from app.startup.composition.configuration import build_scheduler_runtime_config

    expected = str(enabled).lower() == "true"
    config = Settings(MOVIEPILOT_AUTO_UPDATE=enabled, MOVIEPILOT_UPDATE_DEV=not expected)
    assert config.MOVIEPILOT_AUTO_UPDATE is expected
    assert config.MOVIEPILOT_UPDATE_DEV is not expected
    assert build_scheduler_runtime_config(config).auto_update is expected


@pytest.mark.parametrize("value", ["dev", "release", True, False])
def test_update_setting_normalizes_auto_update_on_save(monkeypatch, value):
    """设置写入入口与启动读取入口使用同一套布尔转换规则。"""
    config = Settings(MOVIEPILOT_AUTO_UPDATE=False, MOVIEPILOT_UPDATE_DEV=False)
    monkeypatch.setattr(Settings, "update_env_config", lambda *_args: (True, ""))
    config.update_setting("MOVIEPILOT_AUTO_UPDATE", value)
    assert config.MOVIEPILOT_AUTO_UPDATE is (value is not False)
    assert config.MOVIEPILOT_UPDATE_DEV is False


def test_space_usage_default_path_does_not_read_fsid():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths)

    assert total == 4.0
    assert free == 2.0
    fsid_mock.assert_not_called()


@pytest.mark.parametrize("system_platform", ["darwin", "win32"])
def test_space_usage_opt_in_does_not_read_fsid_outside_linux(system_platform):
    path = MagicMock()
    path.exists.return_value = True
    path.drive = "D:"
    with patch("app.adapters.system.host.sys.platform", system_platform), \
            patch.object(SystemUtils, "is_x86_64") as x86_mock, \
            patch.object(SystemUtils, "is_aarch64") as arm_mock, \
            patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
            patch.object(SystemUtils, "total_space", return_value=2.0), \
            patch.object(SystemUtils, "free_space", return_value=1.0), \
            patch("app.adapters.system.host.os.stat", return_value=MagicMock(st_dev=38)):
        total, free = SystemUtils.space_usage([path], btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0
    x86_mock.assert_not_called()
    arm_mock.assert_not_called()
    fsid_mock.assert_not_called()


def test_space_usage_opt_in_uses_original_behavior_on_unsupported_linux_architecture():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(path): 38 for path in paths}
        with patch("app.adapters.system.host.sys.platform", "linux"), \
                patch.object(SystemUtils, "is_x86_64", return_value=False), \
                patch.object(SystemUtils, "is_aarch64", return_value=False), \
                patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0
    fsid_mock.assert_not_called()


@pytest.mark.parametrize(("is_x86_64", "is_aarch64"), [(True, False), (False, True)])
def test_space_usage_merges_btrfs_subvolumes_with_same_fsid(is_x86_64, is_aarch64, monkeypatch):
    monkeypatch.setattr("app.adapters.system.host.sys.platform", "linux")
    monkeypatch.setattr(SystemUtils, "is_x86_64", lambda: is_x86_64)
    monkeypatch.setattr(SystemUtils, "is_aarch64", lambda: is_aarch64)
    fsid = bytes.fromhex("88e3aff5fa2946d591a55977be984655")
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=[fsid, fsid]), \
                patch.object(SystemUtils, "total_space", return_value=3.49 * 1024 ** 4), \
                patch.object(SystemUtils, "free_space", return_value=1.94 * 1024 ** 4):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 3.49 * 1024 ** 4
    assert free == 1.94 * 1024 ** 4


def test_space_usage_counts_different_btrfs_fsids_separately(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=[b"a" * 16, b"b" * 16]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 4.0
    assert free == 2.0


def test_space_usage_falls_back_to_st_dev_when_fsid_is_unavailable(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 60, str(paths[1]): 60}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", return_value=None), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0


@pytest.mark.parametrize("fsids", [(b"a" * 16, None), (None, b"a" * 16)])
def test_space_usage_keeps_st_dev_dedup_when_fsid_availability_differs(fsids, linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(path): 38 for path in paths}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=fsids), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0


def test_space_usage_keeps_st_dev_dedup_when_fsid_is_consistent(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(path): 38 for path in paths}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=[b"a" * 16, b"a" * 16]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0


def test_space_usage_does_not_merge_different_st_devs_when_one_fsid_is_unavailable(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=[b"a" * 16, None]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 4.0
    assert free == 2.0


def test_space_usage_counts_a_repeated_path_once():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        with patch.object(SystemUtils, "_get_btrfs_fsid", return_value=None), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage([path, path])

    assert total == 2.0
    assert free == 1.0


def test_space_usage_merges_consistent_fsid_observed_within_same_st_dev(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2, \
            tempfile.TemporaryDirectory() as tmp3:
        paths = [Path(tmp1), Path(tmp2), Path(tmp3)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 38, str(paths[2]): 32}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=[None, b"a" * 16, b"a" * 16]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0


def test_space_usage_transitive_merge_is_independent_of_path_order(linux_platform):
    fsid = b"a" * 16
    records = [("first", 38, None), ("bridge", 38, fsid), ("other", 32, fsid)]

    for permutation in itertools.permutations(records):
        paths = [MagicMock(name=name) for name, _, _ in permutation]
        for path in paths:
            path.exists.return_value = True
        dev_by_path = {str(path): record[1] for path, record in zip(paths, permutation)}
        fsid_by_path = {str(path): record[2] for path, record in zip(paths, permutation)}
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=lambda path: fsid_by_path[str(path)]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

        assert total == 2.0
        assert free == 1.0


def test_space_usage_conflicting_fsids_do_not_bridge_independent_groups(linux_platform):
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2, \
            tempfile.TemporaryDirectory() as tmp3, tempfile.TemporaryDirectory() as tmp4:
        paths = [Path(tmp1), Path(tmp2), Path(tmp3), Path(tmp4)]
        dev_by_path = {
            str(paths[0]): 38,
            str(paths[1]): 38,
            str(paths[2]): 32,
            str(paths[3]): 101,
        }
        with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid",
                             side_effect=[b"a" * 16, b"b" * 16, b"a" * 16, b"b" * 16]), \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 6.0
    assert free == 3.0


def test_space_usage_uses_earliest_path_for_each_final_group(linux_platform):
    fsid = b"a" * 16
    paths = [MagicMock(name=name) for name in ("first", "same-dev", "same-fsid", "independent")]
    for path in paths:
        path.exists.return_value = True
    dev_by_path = {str(paths[0]): 38, str(paths[1]): 38, str(paths[2]): 32, str(paths[3]): 101}
    fsid_by_path = {str(paths[0]): None, str(paths[1]): fsid, str(paths[2]): fsid, str(paths[3]): None}

    with patch("app.adapters.system.host.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
            patch.object(SystemUtils, "_get_btrfs_fsid", side_effect=lambda path: fsid_by_path[str(path)]), \
            patch.object(SystemUtils, "total_space", side_effect=[2.0, 3.0]) as total_mock, \
            patch.object(SystemUtils, "free_space", side_effect=[1.0, 1.5]) as free_mock:
        total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 5.0
    assert free == 2.5
    assert total_mock.call_args_list == [call(paths[0]), call(paths[3])]
    assert free_mock.call_args_list == [call(paths[0]), call(paths[3])]


def test_space_usage_keeps_windows_drive_behavior_without_fsid_lookup():
    path = MagicMock()
    path.exists.return_value = True
    path.drive = "D:"
    with patch("app.adapters.system.host.os.name", "nt"), \
            patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
            patch.object(SystemUtils, "total_space", return_value=2.0), \
            patch.object(SystemUtils, "free_space", return_value=1.0):
        total, free = SystemUtils.space_usage([path])

    assert total == 2.0
    assert free == 1.0
    fsid_mock.assert_not_called()


def test_local_storage_usage_forwards_btrfs_fsid_setting():
    from app.modules.filemanager.storages import local as local_storage_module

    download_dir = MagicMock(download_path="/downloads")
    library_dir = MagicMock(library_path="/library")
    with patch.object(local_storage_module.settings, "BTRFS_FSID_DEDUP", True), \
            patch.object(local_storage_module.DirectoryHelper, "get_local_download_dirs",
                         return_value=[download_dir]), \
            patch.object(local_storage_module.DirectoryHelper, "get_local_library_dirs",
                         return_value=[library_dir]), \
            patch.object(SystemUtils, "space_usage", return_value=(4.0, 2.0)) as usage_mock:
        usage = object.__new__(local_storage_module.LocalStorage).usage()

    assert usage.total == 4.0
    assert usage.available == 2.0
    usage_mock.assert_called_once_with(
        [Path("/downloads"), Path("/library")],
        btrfs_fsid_dedup=True,
    )


def test_dashboard_downloader_forwards_btrfs_fsid_setting():
    from app.api.endpoints import dashboard as dashboard_module

    download_dir = MagicMock(download_path="/downloads")
    with patch.object(dashboard_module.DirectoryHelper, "get_local_download_dirs",
                         return_value=[download_dir]), \
            patch.object(SystemUtils, "space_usage", return_value=(4.0, 2.0)) as usage_mock, \
            patch.object(dashboard_module.DashboardChain, "downloader_info", return_value=[]):
        dashboard_module._build_downloader(btrfs_fsid_dedup=True)

    usage_mock.assert_called_once_with(
        [Path("/downloads")],
        btrfs_fsid_dedup=True,
    )
