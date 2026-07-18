import io
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.helper.system import SystemHelper
from app.core.config import settings
from app.utils.system import SystemUtils

# 在任何 mock 生效前保存真实的 open，供测试内需要放行非 /proc/mounts 路径的场景使用
_real_open = open


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


def _fake_open_with_proc_mounts(fake_mounts_content):
    """
    构造一个 open 的 side_effect：命中 /proc/mounts 时返回构造好的内容，
    其它路径放行给真实 open，避免影响测试框架自身的文件读写。
    """
    def _fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            return io.StringIO(fake_mounts_content)
        return _real_open(path, *args, **kwargs)
    return _fake_open


def test_space_usage_dedupes_btrfs_subvolumes_sharing_one_mount_source():
    """
    Btrfs 存储池下，不同共享文件夹通常各自是独立子卷，os.stat 返回的 st_dev
    因此互不相同，但它们在 /proc/mounts 里的设备来源字段是完全一致的（同一个
    /dev/mdX）。这是真实生产环境（群晖 DSM）实测确认过的现象，用它去重比比较
    容量数值更可靠，不受并发写入、剩余空间变化的影响。
    """
    single_disk_total = 3.49 * 1024 ** 4
    single_disk_free = 1.97 * 1024 ** 4

    with tempfile.TemporaryDirectory() as tmp1, \
            tempfile.TemporaryDirectory() as tmp2, \
            tempfile.TemporaryDirectory() as tmp3:
        paths = [Path(tmp1), Path(tmp2), Path(tmp3)]
        fake_mounts = (
            f"/dev/md3 {tmp1} btrfs rw,relatime,subvolid=375 0 0\n"
            f"/dev/md3 {tmp2} btrfs rw,relatime,subvolid=259 0 0\n"
            f"/dev/md3 {tmp3} btrfs rw,relatime,subvolid=806 0 0\n"
        )

        with patch("builtins.open", side_effect=_fake_open_with_proc_mounts(fake_mounts)), \
                patch.object(SystemUtils, "total_space", return_value=single_disk_total), \
                patch.object(SystemUtils, "free_space", return_value=single_disk_free):
            total, free = SystemUtils.space_usage(paths)

    assert total == single_disk_total
    assert free == single_disk_free


def test_space_usage_keeps_different_mount_sources_separate():
    """
    基线正确性：两个真正独立的挂载（不同设备来源）必须分别计入总量，
    即使 st_dev 和容量数值都是虚构的、互不冲突，也不应该被误合并。
    """
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        fake_mounts = (
            f"/dev/md3 {tmp1} btrfs rw,relatime 0 0\n"
            f"/dev/sdb1 {tmp2} ext4 rw,relatime 0 0\n"
        )
        fake_total_by_path = {str(paths[0]): 3.0 * 1024 ** 4, str(paths[1]): 1.0 * 1024 ** 4}
        fake_free_by_path = {str(paths[0]): 1.5 * 1024 ** 4, str(paths[1]): 0.4 * 1024 ** 4}

        with patch("builtins.open", side_effect=_fake_open_with_proc_mounts(fake_mounts)), \
                patch.object(SystemUtils, "total_space", side_effect=lambda p: fake_total_by_path[str(p)]), \
                patch.object(SystemUtils, "free_space", side_effect=lambda p: fake_free_by_path[str(p)]):
            total, free = SystemUtils.space_usage(paths)

    assert total == 3.0 * 1024 ** 4 + 1.0 * 1024 ** 4
    assert free == 1.5 * 1024 ** 4 + 0.4 * 1024 ** 4


def test_read_proc_mounts_unescapes_octal_sequences_in_mount_point():
    """
    /proc/mounts 会把挂载点里的空格等特殊字符转义成八进制序列（如空格 -> \\040），
    _read_proc_mounts 必须先还原转义，否则合法但含特殊字符的挂载点永远匹配不上，
    去重会退化回旧的 st_dev 方式（对应代码审查中指出的"挂载点未解码"问题）。
    """
    fake_mounts = "/dev/md3 /mnt/My\\040Disk btrfs rw,relatime 0 0\n"

    with patch("builtins.open", side_effect=_fake_open_with_proc_mounts(fake_mounts)):
        mounts = SystemUtils._read_proc_mounts()
        source = SystemUtils._match_mount_source(Path("/mnt/My Disk/subdir/video"), mounts)

    assert mounts == [("/dev/md3", "/mnt/My Disk")]
    assert source == "/dev/md3"


def test_match_mount_source_prefers_later_entry_on_equal_length_mount_point():
    """
    Linux 允许在同一路径叠加挂载多层，/proc/mounts 里排在后面的记录才是当前实际
    生效的那一层。两条挂载点长度完全相同的记录，必须以列表中更靠后的为准，
    而不是先出现的那条——否则挂载栈叠加场景下会拿到已经被覆盖、不再生效的旧挂载。
    """
    mounts = [
        ("/dev/overlay-old", "/data"),
        ("/dev/overlay-new", "/data"),
    ]

    source = SystemUtils._match_mount_source(Path("/data/sub"), mounts)

    assert source == "/dev/overlay-new"


def test_space_usage_falls_back_to_st_dev_when_proc_mounts_unavailable():
    """
    /proc/mounts 不可读（比如非 Linux 环境或权限问题）时，_get_mount_source
    应该安静地返回 None，space_usage 平滑回退到原有的 st_dev 去重方式，
    而不是抛异常或者把所有目录错误合并成一块。
    """
    def fake_open_raises(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise OSError("/proc/mounts not available")
        return _real_open(path, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        paths = [Path(tmp1), Path(tmp2)]
        fake_dev_by_path = {str(paths[0]): 9001, str(paths[1]): 9002}

        def fake_stat(path, *_args, **_kwargs):
            stat_result = MagicMock()
            stat_result.st_dev = fake_dev_by_path[str(path)]
            return stat_result

        with patch("builtins.open", side_effect=fake_open_raises), \
                patch("app.utils.system.os.stat", side_effect=fake_stat), \
                patch.object(SystemUtils, "total_space", return_value=1.0 * 1024 ** 4), \
                patch.object(SystemUtils, "free_space", return_value=0.5 * 1024 ** 4):
            total, free = SystemUtils.space_usage(paths)

    assert total == 2.0 * 1024 ** 4
    assert free == 1.0 * 1024 ** 4
