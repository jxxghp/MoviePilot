import os
import signal
import threading
import time
from pathlib import Path
from typing import Tuple

import docker

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas import ConfigChangeEventData
from app.schemas.types import EventType
from app.utils.system import SystemUtils


class SystemHelper:
    """
    系统工具类，提供系统相关的操作和判断
    """

    __system_flag_file = "/var/log/nginx/__moviepilot__"

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新日志设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in ['DEBUG', 'LOG_LEVEL', 'LOG_MAX_FILE_SIZE', 'LOG_BACKUP_COUNT',
                                  'LOG_FILE_FORMAT', 'LOG_CONSOLE_FORMAT']:
            return
        logger.info("配置变更，更新日志设置...")
        logger.update_loggers()

    @staticmethod
    def can_restart() -> bool:
        """
        判断是否可以内部重启
        """
        return (
                Path("/var/run/docker.sock").exists()
                or settings.DOCKER_CLIENT_API != "tcp://127.0.0.1:38379"
        )

    @staticmethod
    def _get_container_id() -> str:
        """
        获取当前容器ID
        """
        container_id = None
        try:
            with open("/proc/self/mountinfo", "r") as f:
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
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
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
    def restart() -> Tuple[bool, str]:
        """
        执行Docker重启操作
        """
        if not SystemUtils.is_docker():
            return False, "非Docker环境，无法重启！"

        try:
            # 检查容器是否配置了自动重启策略
            has_restart_policy = SystemHelper._check_restart_policy()
            if has_restart_policy:
                # 有重启策略，使用优雅退出方式
                logger.info("检测到容器配置了自动重启策略，使用优雅重启方式...")
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
            # 降级为Docker API重启
            logger.warning("降级为Docker API重启...")
            return SystemHelper._docker_api_restart()

    @staticmethod
    def _start_graceful_shutdown_monitor():
        """
        启动优雅退出超时监控
        如果30秒内进程没有退出，则使用Docker API强制重启
        """

        def monitor_thread():
            time.sleep(30)  # 等待30秒
            logger.warning("优雅退出超时30秒，使用Docker API强制重启...")
            try:
                SystemHelper._docker_api_restart()
            except Exception as e:
                logger.error(f"强制重启失败: {str(e)}")

        # 在后台线程中启动监控
        thread = threading.Thread(target=monitor_thread, daemon=True)
        thread.start()

    @staticmethod
    def _docker_api_restart() -> Tuple[bool, str]:
        """
        使用Docker API重启容器，并尝试优雅停止
        """
        try:
            # 创建 Docker 客户端
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
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
            if SystemUtils.is_docker():
                Path(self.__system_flag_file).touch(exist_ok=True)
        except Exception as e:
            print(f"设置系统修改标志失败: {str(e)}")

    def is_system_reset(self) -> bool:
        """
        检查系统是否已被重置
        :return: 如果系统已重置，返回 True；否则返回 False
        """
        if SystemUtils.is_docker():
            return not Path(self.__system_flag_file).exists()
        return False
