import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple

import psutil

from app.foundation.environment import is_docker, is_frozen, is_windows
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.runtime.settings import get_runtime_setting


class SystemHelper(ConfigReloadMixin):
    """
    系统工具类，提供系统相关的操作和判断
    """
    CONFIG_WATCH = {
        "DEBUG",
        "LOG_LEVEL",
        "LOG_MAX_FILE_SIZE",
        "LOG_BACKUP_COUNT",
        "LOG_FILE_FORMAT",
        "LOG_CONSOLE_FORMAT",
    }

    __system_flag_file = "/var/log/nginx/__moviepilot__"
    __local_backend_runtime_file = (
        get_runtime_setting('TEMP_PATH') / "moviepilot.runtime.json"
    )
    __local_restart_log_file = (
        get_runtime_setting('LOG_PATH') / "moviepilot.restart.stdout.log"
    )
    __one_shot_dev_update_flag_file = (
        get_runtime_setting('TEMP_PATH') / "moviepilot.pending_dev_update"
    )
    __prepared_update_manifest = (
        get_runtime_setting('TEMP_PATH') / "moviepilot-update/install.json"
    )
    __supervisor_config = Path("/etc/supervisor/supervisord.conf")
    __supervisorctl = Path("/usr/bin/supervisorctl")
    __supervisor_socket = Path("/run/moviepilot/supervisor.sock")
    __supervisor_update_worker = "moviepilot-update-worker"

    def on_config_changed(self):
        """配置变化后重新应用日志设置。"""
        logger.update_loggers()

    def get_reload_name(self):
        """返回配置重载日志使用的组件名称。"""
        return "日志设置"

    @staticmethod
    def can_restart() -> bool:
        """判断当前部署是否具备宿主无关的进程重启能力。"""
        return (
            (
                is_docker()
                and SystemHelper.__supervisor_config.exists()
                and SystemHelper.__supervisorctl.exists()
                and SystemHelper.__supervisor_socket.exists()
            )
            or SystemHelper._is_local_cli_managed()
            or (is_windows() and not is_frozen())
        )

    @staticmethod
    def _load_runtime_file(path: Path) -> Optional[dict]:
        """安全读取本地进程运行状态文件。"""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_local_cli_managed() -> bool:
        """判断当前进程是否由本地 CLI 运行状态文件管理。"""
        runtime = SystemHelper._load_runtime_file(SystemHelper.__local_backend_runtime_file)
        if not runtime:
            return False

        pid = runtime.get("pid")
        create_time = runtime.get("create_time")
        if not pid:
            return False

        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False

        if pid != os.getpid():
            return False

        if create_time is None:
            return True

        try:
            current_process = psutil.Process(os.getpid())
            return abs(current_process.create_time() - float(create_time)) <= 2
        except (psutil.Error, TypeError, ValueError):
            return False

    @staticmethod
    def queue_one_shot_dev_update() -> Tuple[bool, str]:
        """写入一次性 Dev 更新标记，供本次重启的启动流程消费。"""
        try:
            SystemHelper.__one_shot_dev_update_flag_file.parent.mkdir(
                parents=True, exist_ok=True
            )
            SystemHelper.__one_shot_dev_update_flag_file.write_text(
                "dev", encoding="utf-8"
            )
            return True, ""
        except OSError as err:
            logger.error(f"写入一次性 Dev 更新标记失败: {err}")
            return False, f"写入一次性 Dev 更新标记失败：{err}"

    @staticmethod
    def consume_one_shot_dev_update() -> bool:
        """读取并删除一次性 Dev 更新标记，确保普通重启不会重复更新。"""
        path = SystemHelper.__one_shot_dev_update_flag_file
        if not path.exists():
            return False
        try:
            mode = path.read_text(encoding="utf-8", errors="replace").strip().lower()
            path.unlink(missing_ok=True)
        except OSError as err:
            logger.warning(f"消费一次性 Dev 更新标记失败: {err}")
            return False
        return mode == "dev"

    @staticmethod
    def clear_one_shot_dev_update() -> None:
        """重启失败时撤销尚未消费的一次性 Dev 更新。"""
        try:
            SystemHelper.__one_shot_dev_update_flag_file.unlink(missing_ok=True)
        except OSError as err:
            logger.warning(f"清理一次性 Dev 更新标记失败: {err}")

    @staticmethod
    def _spawn_local_restart_helper() -> None:
        """启动脱离当前进程的本地 CLI 重启助手。"""
        helper_code = (
            "import os, subprocess, sys, time;"
            "time.sleep(1.0);"
            "cmd=[sys.executable, '-m', 'app.cli', 'restart', '--force', '--stop-timeout', '30', '--start-timeout', '60'];"
            "subprocess.run(cmd, cwd=os.environ.get('MOVIEPILOT_ROOT'), env=os.environ.copy(), check=False)"
        )
        env = os.environ.copy()
        root_path = get_runtime_setting('ROOT_PATH')
        env["MOVIEPILOT_ROOT"] = str(root_path)
        env["PYTHONUNBUFFERED"] = "1"

        SystemHelper.__local_restart_log_file.parent.mkdir(parents=True, exist_ok=True)
        with SystemHelper.__local_restart_log_file.open("a", encoding="utf-8") as log_handle:
            kwargs = {
                "cwd": str(root_path),
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "close_fds": True,
                "env": env,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen([sys.executable, "-c", helper_code], **kwargs)
        logger.info(f"已创建本地 CLI 重启任务，辅助进程 PID: {process.pid}")

    @staticmethod
    def _schedule_supervisor_restart() -> None:
        """延迟调用本地 supervisor，确保重启接口有机会完成响应。"""
        SystemHelper._schedule_supervisor_command("restart", "all")

    @staticmethod
    def _schedule_supervisor_shutdown() -> None:
        """延迟关闭 supervisor，让容器入口重新执行更新和启动准备流程。"""
        SystemHelper._schedule_supervisor_command("shutdown")

    @staticmethod
    def _schedule_supervisor_command(action: str, target: Optional[str] = None) -> None:
        """延迟调用本地 supervisor 控制命令，确保重启接口有机会完成响应。"""
        def run_command() -> None:
            command = [
                str(SystemHelper.__supervisorctl),
                "-c",
                str(SystemHelper.__supervisor_config),
                action,
            ]
            if target is not None:
                command.append(target)
            try:
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as err:
                logger.error(f"调用 supervisor {action} 失败: {err}")

        restart_timer = threading.Timer(0.5, run_command)
        restart_timer.daemon = True
        restart_timer.start()

    @staticmethod
    def _windows_restart() -> tuple[bool, str]:
        """
        执行Windows重启操作
        """
        def cmd(command: list[str]) -> Tuple[bool, str]:
            """通过 Windows 命令解释器执行重启命令。"""
            try:
                subprocess.run(command, shell=True)
                return True, ""
            except Exception as error:
                return False, f"cmd命令{command}执行失败.原因:{str(error)}"

        mp_exe = str(Path(__file__).parents[3].parent / "MoviePilot-V3.exe")
        if not Path(mp_exe).exists():
            return False, f"{mp_exe} 文件不存在, 无法执行重启"
        success, message = cmd(["start", "", mp_exe, "-c", "restart"])
        if not success:
            return False, message
        return True, ""

    @staticmethod
    def restart() -> Tuple[bool, str]:
        """执行当前部署支持的受管重启流程。"""
        if not is_frozen() and is_windows():
            success, message = SystemHelper._windows_restart()
            return success, message
        if not is_docker():
            if not SystemHelper._is_local_cli_managed():
                return False, "当前实例不是由 moviepilot CLI 启动，无法执行内建重启！"
            try:
                SystemHelper._spawn_local_restart_helper()
                # 复用与 Docker 相同的优雅退出路径，确保当前后端进程真正结束。
                os.kill(os.getpid(), signal.SIGTERM)
                return True, ""
            except Exception as err:
                logger.error(f"本地 CLI 重启失败: {str(err)}")
                return False, f"本地 CLI 重启失败：{str(err)}"

        if not (
            SystemHelper.__supervisor_config.exists()
            and SystemHelper.__supervisorctl.exists()
            and SystemHelper.__supervisor_socket.exists()
        ):
            return False, "容器内 supervisor 未安装"
        if SystemHelper.__prepared_update_manifest.is_file():
            logger.info("检测到已确认的更新包，请求 root 更新 worker 替换程序目录")
            SystemHelper._schedule_supervisor_command(
                "start", SystemHelper.__supervisor_update_worker
            )
        elif SystemHelper.__one_shot_dev_update_flag_file.is_file():
            logger.info("检测到一次性 Dev 更新，请求 supervisor 关闭并重新执行容器启动流程")
            SystemHelper._schedule_supervisor_shutdown()
        else:
            logger.info("请求容器内 supervisor 重启前后端服务")
            SystemHelper._schedule_supervisor_restart()
        return True, ""

    @staticmethod
    def upgrade_dev() -> Tuple[bool, str]:
        """保留原 Dev 模式：重启后跟踪当前 v3 开发分支。"""
        queued, message = SystemHelper.queue_one_shot_dev_update()
        if not queued:
            return False, message
        ret, message = SystemHelper.restart()
        if not ret:
            SystemHelper.clear_one_shot_dev_update()
            return False, message
        return True, "已安排 Dev 更新并重启"

    def set_system_modified(self):
        """
        设置系统已修改标志
        """
        try:
            if is_docker():
                Path(self.__system_flag_file).touch(exist_ok=True)
        except Exception as e:
            print(f"设置系统修改标志失败: {str(e)}")

    def is_system_reset(self) -> bool:
        """
        检查系统是否已被重置
        :return: 如果系统已重置，返回 True；否则返回 False
        """
        if is_docker():
            return not Path(self.__system_flag_file).exists()
        return False
