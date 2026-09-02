import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import docker
import psutil

from app.runtime.settings import get_runtime_setting
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.foundation.environment import is_windows,is_frozen,is_docker


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
    __docker_restart_intent_file = (
        get_runtime_setting('TEMP_PATH') / "moviepilot.intentional_restart"
    )
    __graceful_shutdown_monitor_lock = threading.Lock()
    __graceful_shutdown_monitor: Optional[threading.Thread] = None

    def on_config_changed(self):
        """配置变化后重新应用日志设置。"""
        logger.update_loggers()

    def get_reload_name(self):
        """返回配置重载日志使用的组件名称。"""
        return "日志设置"

    @staticmethod
    def can_restart() -> bool:
        """
        判断是否可以内部重启
        """
        return is_docker() or SystemHelper._is_local_cli_managed() or (is_windows() and not is_frozen())

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
    def _get_container_id() -> str:
        """
        获取当前容器ID
        """
        container_id = None
        try:
            with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
                index_resolv_conf = data.find("resolv.conf")
                if index_resolv_conf != -1:
                    index_second_slash = data.rfind("/", 0, index_resolv_conf)
                    index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                    container_id = data[index_first_slash:index_second_slash]
                    if len(container_id) < 20:
                        index_resolv_conf = data.find("/sys/fs/cgroup/devices")
                        if index_resolv_conf != -1:
                            index_second_slash = data.rfind(" ", 0, index_resolv_conf)
                            index_first_slash = (
                                    data.rfind("/", 0, index_second_slash) + 1
                            )
                            container_id = data[index_first_slash:index_second_slash]
        except Exception as e:
            logger.debug(f"获取容器ID失败: {str(e)}")
        return container_id.strip() if container_id else None

    @staticmethod
    def _check_restart_policy() -> bool:
        """
        检查当前容器是否配置了自动重启策略
        """
        try:
            # 获取当前容器ID
            container_id = SystemHelper._get_container_id()
            if not container_id:
                return False

            # 创建 Docker 客户端
            client = docker.DockerClient(
                base_url=get_runtime_setting('DOCKER_CLIENT_API')
            )
            # 获取容器信息
            container = client.containers.get(container_id)
            restart_policy = container.attrs.get('HostConfig', {}).get('RestartPolicy', {})
            policy_name = restart_policy.get('Name', 'no')
            # 检查是否有有效的重启策略
            auto_restart_policies = ['always', 'unless-stopped', 'on-failure']
            has_restart_policy = policy_name in auto_restart_policies

            logger.info(f"容器重启策略: {policy_name}, 支持自动重启: {has_restart_policy}")
            return has_restart_policy

        except Exception as e:
            logger.warning(f"检查重启策略失败: {str(e)}")
            return False

    @staticmethod
    def _mark_docker_intentional_restart() -> None:
        """写入 Docker 主动重启标记，避免守护逻辑误判崩溃。"""
        try:
            SystemHelper.__docker_restart_intent_file.parent.mkdir(
                parents=True, exist_ok=True
            )
            SystemHelper.__docker_restart_intent_file.write_text(
                str(os.getpid()), encoding="utf-8"
            )
        except OSError as err:
            logger.warning(f"写入内置重启标记失败: {err}")

    @staticmethod
    def _clear_docker_intentional_restart() -> None:
        """清理 Docker 主动重启标记。"""
        try:
            SystemHelper.__docker_restart_intent_file.unlink(missing_ok=True)
        except OSError as err:
            logger.warning(f"清理内置重启标记失败: {err}")

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
        """
        执行Docker重启操作
        """
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

        try:
            # 检查容器是否配置了自动重启策略
            has_restart_policy = SystemHelper._check_restart_policy()
            if has_restart_policy:
                # 有重启策略，使用优雅退出方式
                logger.info("检测到容器配置了自动重启策略，使用优雅重启方式...")
                SystemHelper._mark_docker_intentional_restart()
                # 启动优雅退出超时监控
                SystemHelper._start_graceful_shutdown_monitor()
                # 发送SIGTERM信号给当前进程，触发优雅停止
                os.kill(os.getpid(), signal.SIGTERM)
                return True, ""
            else:
                # 没有重启策略，使用Docker API强制重启
                logger.info("容器未配置自动重启策略，使用Docker API重启...")
                return SystemHelper._docker_api_restart()
        except Exception as err:
            logger.error(f"重启失败: {str(err)}")
            SystemHelper._clear_docker_intentional_restart()
            # 降级为Docker API重启
            logger.warning("降级为Docker API重启...")
            return SystemHelper._docker_api_restart()

    @staticmethod
    def upgrade_dev() -> Tuple[bool, str]:
        """保留原 Dev 模式：重启后跟踪当前 v3 开发分支。"""
        configured_mode = str(
            get_runtime_setting('MOVIEPILOT_AUTO_UPDATE') or ""
        ).strip().lower()
        if configured_mode != "dev":
            queued, message = SystemHelper.queue_one_shot_dev_update()
            if not queued:
                return False, message
        ret, message = SystemHelper.restart()
        if not ret:
            SystemHelper.clear_one_shot_dev_update()
            return False, message
        return True, "已安排 Dev 更新并重启"

    @staticmethod
    def _start_graceful_shutdown_monitor():
        """
        启动唯一的优雅退出超时监控。

        如果 180 秒内进程没有退出，则使用 Docker API 强制重启；重复重启请求
        复用当前 monitor，避免并行触发多次容器重启。
        """

        def monitor_thread():
            try:
                time.sleep(180)
                logger.warning("优雅退出超时180秒，使用Docker API强制重启...")
                try:
                    SystemHelper._docker_api_restart()
                except Exception as e:
                    logger.error(f"强制重启失败: {str(e)}")
            finally:
                with SystemHelper.__graceful_shutdown_monitor_lock:
                    if (
                        SystemHelper.__graceful_shutdown_monitor
                        is threading.current_thread()
                    ):
                        SystemHelper.__graceful_shutdown_monitor = None

        with SystemHelper.__graceful_shutdown_monitor_lock:
            running = SystemHelper.__graceful_shutdown_monitor
            if running is not None and running.is_alive():
                logger.debug("优雅退出超时监控已在运行，跳过重复启动")
                return
            thread = threading.Thread(
                target=monitor_thread,
                name="MoviePilot-GracefulRestartFallback",
                daemon=True,
            )
            SystemHelper.__graceful_shutdown_monitor = thread
            try:
                thread.start()
            except BaseException:
                SystemHelper.__graceful_shutdown_monitor = None
                raise

    @staticmethod
    def _docker_api_restart() -> Tuple[bool, str]:
        """
        使用Docker API重启容器，并尝试优雅停止
        """
        try:
            # 创建 Docker 客户端
            client = docker.DockerClient(
                base_url=get_runtime_setting('DOCKER_CLIENT_API')
            )
            container_id = SystemHelper._get_container_id()
            if not container_id:
                return False, "获取容器ID失败！"
            # 重启容器
            client.containers.get(container_id).restart()
            return True, ""
        except Exception as docker_err:
            return False, f"重启时发生错误：{str(docker_err)}"

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
