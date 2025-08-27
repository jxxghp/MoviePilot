from pathlib import Path
from typing import Tuple
import os
import signal

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
    def _get_docker_client():
        """
        获取Docker客户端，优先使用Unix socket
        """
        # 优先使用Unix socket
        if Path("/var/run/docker.sock").exists():
            return docker.DockerClient(base_url="unix://var/run/docker.sock")
        # 回退到配置的API地址
        return docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)

    @staticmethod
    def _test_docker_connection(client) -> bool:
        """
        测试Docker连接是否可用
        """
        try:
            client.ping()
            return True
        except Exception:
            return False

    @staticmethod
    def restart() -> Tuple[bool, str]:
        """
        执行Docker重启操作
        """
        if not SystemUtils.is_docker():
            return False, "非Docker环境，无法重启！"

        logger.info("正在重启容器...")
        return SystemHelper._docker_api_restart()

    @staticmethod
    def _docker_api_restart() -> Tuple[bool, str]:
        """
        使用Docker API重启容器，并尝试优雅停止
        """
        try:
            # 创建 Docker 客户端
            client = SystemHelper._get_docker_client()
            
            # 测试Docker连接
            if not SystemHelper._test_docker_connection(client):
                # 如果Docker API不可用，尝试优雅退出
                logger.warning("Docker API不可用，尝试优雅退出...")
                return SystemHelper._graceful_exit()
            
            container_id = SystemHelper._get_container_id()
            if not container_id:
                return False, "获取容器ID失败！"
            
            # 获取容器对象
            container = client.containers.get(container_id)
            
            # 检查容器状态
            container.reload()
            if container.status != 'running':
                return False, f"容器状态异常：{container.status}"
            
            # 重启容器
            logger.info(f"正在重启容器 {container_id}...")
            container.restart()
            return True, ""
        except docker.errors.NotFound:
            return False, "容器不存在或无法访问！"
        except docker.errors.APIError as e:
            logger.warning(f"Docker API错误，尝试优雅退出: {str(e)}")
            return SystemHelper._graceful_exit()
        except Exception as docker_err:
            logger.warning(f"重启时发生错误，尝试优雅退出: {str(docker_err)}")
            return SystemHelper._graceful_exit()

    @staticmethod
    def _graceful_exit() -> Tuple[bool, str]:
        """
        优雅退出，依赖容器的重启策略
        """
        try:
            logger.info("执行优雅退出，依赖容器重启策略...")
            # 发送SIGTERM信号给当前进程
            os.kill(os.getpid(), signal.SIGTERM)
            return True, ""
        except Exception as e:
            return False, f"优雅退出失败: {str(e)}"

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
