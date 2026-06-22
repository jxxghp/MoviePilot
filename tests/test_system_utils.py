import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

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
