from pathlib import Path
from typing import Tuple

import docker

from app.core.config import settings
from app.utils.system import SystemUtils


class SystemHelper:

    @staticmethod
    def can_restart() -> bool:
        """
        判断是否可以内部重启
        """
        return Path("/var/run/docker.sock").exists()

    @staticmethod
    def restart() -> Tuple[bool, str]:
        """
        执行Docker重启操作
        """
        if not SystemUtils.is_docker():
            return False, "非Docker环境，无法重启！"
        try:
            # 创建 Docker 客户端
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
            # 获取当前容器的 ID
            container_id = None
            with open('/proc/self/mountinfo', 'r') as f:
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
                            index_first_slash = data.rfind("/", 0, index_second_slash) + 1
                            container_id = data[index_first_slash:index_second_slash]
            if not container_id:
                return False, "获取容器ID失败！"
            # 重启当前容器
            client.containers.get(container_id.strip()).restart()
            return True, ""
        except Exception as err:
            print(str(err))
            return False, f"重启时发生错误：{str(err)}"
