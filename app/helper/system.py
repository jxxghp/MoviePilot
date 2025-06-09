import os
import signal
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
            # 创建 Docker 客户端
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
            container_id = SystemHelper._get_container_id()
            if not container_id:
                return False
            
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
            
            container = client.containers.get(container_id)
            
            # 尝试优雅停止：先发送SIGTERM信号，给容器30秒时间优雅停止
            try:
                logger.info("发送SIGTERM信号，尝试优雅停止...")
                container.kill(signal='SIGTERM')
                
                # 等待容器优雅停止，最多等待30秒
                for i in range(30):
                    container.reload()
                    if container.status != 'running':
                        logger.info(f"容器已优雅停止 (耗时 {i+1} 秒)")
                        break
                    time.sleep(1)
                else:
                    # 30秒后仍未停止，强制停止
                    logger.warning("优雅停止超时，强制停止容器...")
                    container.kill(signal='SIGKILL')
                    
            except Exception as stop_err:
                logger.warning(f"优雅停止失败: {str(stop_err)}")
                # 直接重启
                container.restart()
                return True, ""
            
            # 启动容器
            logger.info("启动容器...")
            container.start()
            return True, ""
            
        except Exception as docker_err:
            print(str(docker_err))
            return False, f"重启时发生错误：{str(docker_err)}"
