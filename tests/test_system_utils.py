import errno
import itertools
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

import pytest

from app.helper.system import SystemHelper
from app.core.config import ConfigModel, settings
from app.utils.system import SystemUtils


class SystemUtilsTest(TestCase):

    def test_execute_with_subprocess_keeps_stdout_when_command_fails(self):
        """
        命令失败时如果原因只写入 stdout，也需要回传给调用方用于错误提示。
        """
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["pip", "check"],
            output="demo requires pkg>=2, but you have pkg 1\n",
            stderr="",
        )

        with patch("app.utils.system.subprocess.run", side_effect=error):
            success, message = SystemUtils.execute_with_subprocess(["pip", "check"])

        self.assertFalse(success)
        self.assertIn("返回码：1", message)
        self.assertIn("标准输出：demo requires pkg>=2, but you have pkg 1", message)

    def test_execute_with_subprocess_reports_empty_failure_output(self):
        """
        命令失败且没有任何输出时，给出明确占位信息，避免错误原因看起来被截断。
        """
        error = subprocess.CalledProcessError(
            returncode=2,
            cmd=["pip", "check"],
            output="",
            stderr="",
        )

        with patch("app.utils.system.subprocess.run", side_effect=error):
            success, message = SystemUtils.execute_with_subprocess(["pip", "check"])

        self.assertFalse(success)
        self.assertIn("返回码：2", message)
        self.assertIn("无标准输出或错误输出", message)


class SystemHelperRestartTest(TestCase):

    def test_docker_restart_policy_marks_intent_before_sigterm(self):
        """
        Docker 内置重启走优雅退出时，应写入意图标记，避免 entrypoint 误进入 doctor 保活。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            original_config_dir = settings.CONFIG_DIR
            original_intent_file = SystemHelper._SystemHelper__docker_restart_intent_file
            settings.CONFIG_DIR = temp_dir
            SystemHelper._SystemHelper__docker_restart_intent_file = (
                settings.TEMP_PATH / "moviepilot.intentional_restart"
            )
            try:
                with patch("app.helper.system.SystemUtils.is_docker", return_value=True), \
                        patch.object(SystemHelper, "_check_restart_policy", return_value=True), \
                        patch.object(SystemHelper, "_start_graceful_shutdown_monitor"), \
                        patch("app.helper.system.os.kill") as kill_mock:
                    ret, msg = SystemHelper.restart()

                self.assertTrue(ret)
                self.assertEqual(msg, "")
                self.assertTrue((settings.TEMP_PATH / "moviepilot.intentional_restart").exists())
                kill_mock.assert_called_once()
            finally:
                SystemHelper._SystemHelper__docker_restart_intent_file = original_intent_file
                settings.CONFIG_DIR = original_config_dir


def test_execute_with_subprocess_passes_env_to_subprocess():
    with patch("app.utils.system.subprocess.run") as run_mock:
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
    with patch("app.utils.system.subprocess.run", side_effect=error) as run_mock:
        success, message = SystemUtils.execute_with_subprocess(
            command,
            safe_command=["pip", "install", "-i", "https://mirror.example/simple"],
        )

    assert not success
    assert "https://mirror.example/simple" in message
    assert "user:pass" not in message
    assert run_mock.call_args.args[0] == command


def test_execute_with_subprocess_redacts_userinfo_from_stdout_and_stderr():
    error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["pip", "install"],
        output="Looking in indexes: https://user:pass@mirror.example/simple",
        stderr="Proxy failed: http://proxy_user:proxy_pass@proxy.example:7890",
    )

    with patch("app.utils.system.subprocess.run", side_effect=error):
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

    with patch("app.utils.system.subprocess.run", side_effect=error):
        success, message = SystemUtils.execute_with_subprocess(["pip", "install"])

    assert not success
    assert "socks5://proxy.example:7890" in message
    assert "git+https://example.com/org/repo.git" in message
    assert "proxy_user:proxy_pass" not in message
    assert "git_user:git_pass" not in message


def test_execute_with_subprocess_redacts_success_output_userinfo():
    with patch("app.utils.system.subprocess.run") as run_mock:
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
        "app.utils.system.subprocess.run",
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
    monkeypatch.setattr("app.utils.system.sys.platform", "linux")
    monkeypatch.setattr(SystemUtils, "is_x86_64", lambda: True)
    monkeypatch.setattr(SystemUtils, "is_aarch64", lambda: False)


@pytest.mark.parametrize("num_devices", [1, 2])
def test_get_btrfs_fsid_reads_kernel_result_and_closes_fd(num_devices, linux_platform):
    fsid = bytes.fromhex("88e3aff5fa2946d591a55977be984655")
    with patch("app.utils.system.os.open", return_value=42), \
            patch("app.utils.system.fcntl.ioctl", side_effect=_fill_fs_info(fsid, num_devices)), \
            patch("app.utils.system.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result == fsid
    close_mock.assert_called_once_with(42)


@pytest.mark.parametrize("error_number", [errno.ENOTTY, errno.EACCES, errno.EPERM, errno.EINVAL])
def test_get_btrfs_fsid_falls_back_on_expected_ioctl_errors(error_number, linux_platform):
    with patch("app.utils.system.os.open", return_value=42), \
            patch("app.utils.system.fcntl.ioctl", side_effect=OSError(error_number, os.strerror(error_number))), \
            patch("app.utils.system.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    close_mock.assert_called_once_with(42)


def test_get_btrfs_fsid_falls_back_when_directory_cannot_be_opened(linux_platform):
    with patch("app.utils.system.os.open", side_effect=OSError(errno.EACCES, "denied")), \
            patch("app.utils.system.os.close") as close_mock:
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
    with patch("app.utils.system.os.open", return_value=42), \
            patch("app.utils.system.fcntl.ioctl", side_effect=_fill_fs_info(fsid, num_devices, truncate)), \
            patch("app.utils.system.os.close") as close_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    close_mock.assert_called_once_with(42)


def test_get_btrfs_fsid_is_disabled_outside_linux():
    with patch("app.utils.system.sys.platform", "darwin"), \
            patch("app.utils.system.os.open") as open_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    open_mock.assert_not_called()


def test_get_btrfs_fsid_is_disabled_on_unsupported_linux_architecture():
    with patch("app.utils.system.sys.platform", "linux"), \
            patch.object(SystemUtils, "is_x86_64", return_value=False), \
            patch.object(SystemUtils, "is_aarch64", return_value=False), \
            patch("app.utils.system.os.open") as open_mock:
        result = SystemUtils._get_btrfs_fsid(Path("/data"))

    assert result is None
    open_mock.assert_not_called()


def test_btrfs_fsid_dedup_setting_is_opt_in():
    assert ConfigModel().BTRFS_FSID_DEDUP is False
    assert ConfigModel(BTRFS_FSID_DEDUP="true").BTRFS_FSID_DEDUP is True


def test_space_usage_default_path_does_not_read_fsid():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
    with patch("app.utils.system.sys.platform", system_platform), \
            patch.object(SystemUtils, "is_x86_64") as x86_mock, \
            patch.object(SystemUtils, "is_aarch64") as arm_mock, \
            patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
            patch.object(SystemUtils, "total_space", return_value=2.0), \
            patch.object(SystemUtils, "free_space", return_value=1.0), \
            patch("app.utils.system.os.stat", return_value=MagicMock(st_dev=38)):
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
        with patch("app.utils.system.sys.platform", "linux"), \
                patch.object(SystemUtils, "is_x86_64", return_value=False), \
                patch.object(SystemUtils, "is_aarch64", return_value=False), \
                patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
                patch.object(SystemUtils, "_get_btrfs_fsid") as fsid_mock, \
                patch.object(SystemUtils, "total_space", return_value=2.0), \
                patch.object(SystemUtils, "free_space", return_value=1.0):
            total, free = SystemUtils.space_usage(paths, btrfs_fsid_dedup=True)

    assert total == 2.0
    assert free == 1.0
    fsid_mock.assert_not_called()


@pytest.mark.parametrize(("is_x86_64", "is_aarch64"), [(True, False), (False, True)])
def test_space_usage_merges_btrfs_subvolumes_with_same_fsid(is_x86_64, is_aarch64, monkeypatch):
    monkeypatch.setattr("app.utils.system.sys.platform", "linux")
    monkeypatch.setattr(SystemUtils, "is_x86_64", lambda: is_x86_64)
    monkeypatch.setattr(SystemUtils, "is_aarch64", lambda: is_aarch64)
    fsid = bytes.fromhex("88e3aff5fa2946d591a55977be984655")
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        dev_by_path = {str(paths[0]): 38, str(paths[1]): 32}
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
        with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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

    with patch("app.utils.system.os.stat", side_effect=_fake_stat_with_devs(dev_by_path)), \
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
    with patch("app.utils.system.os.name", "nt"), \
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
    with patch.object(dashboard_module.settings, "BTRFS_FSID_DEDUP", True), \
            patch.object(dashboard_module.DirectoryHelper, "get_local_download_dirs",
                         return_value=[download_dir]), \
            patch.object(SystemUtils, "space_usage", return_value=(4.0, 2.0)) as usage_mock, \
            patch.object(dashboard_module.DashboardChain, "downloader_info", return_value=[]):
        dashboard_module._build_downloader()

    usage_mock.assert_called_once_with(
        [Path("/downloads")],
        btrfs_fsid_dedup=True,
    )
