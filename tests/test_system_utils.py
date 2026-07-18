import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.helper.system import SystemHelper
from app.core.config import settings
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


def test_space_usage_dedupes_btrfs_subvolumes_sharing_one_pool():
    """
    Btrfs 存储池下，不同共享文件夹通常各自是独立子卷，os.stat 返回的 st_dev
    因此互不相同，但它们汇报的池总容量应该完全一致。仅靠 st_dev 去重会把同一块
    物理磁盘误判成多块不同磁盘，导致总容量被重复累加。
    """
    single_disk_total = 3.49 * 1024 ** 4
    single_disk_free = 1.97 * 1024 ** 4

    with tempfile.TemporaryDirectory() as tmp1, \
            tempfile.TemporaryDirectory() as tmp2, \
            tempfile.TemporaryDirectory() as tmp3:
        paths = [Path(tmp1), Path(tmp2), Path(tmp3)]
        fake_dev_by_path = {str(paths[0]): 1001, str(paths[1]): 1002, str(paths[2]): 1003}

        def fake_stat(path, *_args, **_kwargs):
            stat_result = MagicMock()
            stat_result.st_dev = fake_dev_by_path[str(path)]
            return stat_result

        with patch("app.utils.system.os.stat", side_effect=fake_stat), \
                patch.object(SystemUtils, "total_space", return_value=single_disk_total), \
                patch.object(SystemUtils, "free_space", return_value=single_disk_free):
            total, free = SystemUtils.space_usage(paths)

    assert total == single_disk_total
    assert free == single_disk_free


def test_space_usage_does_not_merge_two_disks_with_same_total_but_different_free():
    """
    两块型号、分区方式相同的独立物理磁盘可能恰好总容量完全相等，但此刻的剩余空间
    几乎不会也恰好一致。回归保护：仅总容量相同不应被误判为同一块磁盘，必须总容量
    和剩余空间同时相同才去重，否则会把两块真正独立的磁盘错误合并，反而低估总容量。
    """
    same_total = 4.0 * 1024 ** 4

    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        fake_dev_by_path = {str(paths[0]): 2001, str(paths[1]): 2002}
        fake_free_by_path = {str(paths[0]): 1.0 * 1024 ** 4, str(paths[1]): 2.5 * 1024 ** 4}

        def fake_stat(path, *_args, **_kwargs):
            stat_result = MagicMock()
            stat_result.st_dev = fake_dev_by_path[str(path)]
            return stat_result

        def fake_free_space(path):
            return fake_free_by_path[str(path)]

        with patch("app.utils.system.os.stat", side_effect=fake_stat), \
                patch.object(SystemUtils, "total_space", return_value=same_total), \
                patch.object(SystemUtils, "free_space", side_effect=fake_free_space):
            total, free = SystemUtils.space_usage(paths)

    assert total == same_total * 2
    assert free == 1.0 * 1024 ** 4 + 2.5 * 1024 ** 4
