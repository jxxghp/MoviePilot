"""系统钩子域的能力端口客户端。"""

from __future__ import annotations

from typing import Dict

from app.application.orchestration.ports.dispatch import CapabilityPorts


class SystemPorts(CapabilityPorts):
    """菜单命令注册、定时服务与缓存清理的能力端口。"""

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        注册菜单命令
        """
        self._dispatch.broadcast("register_commands", commands=commands)

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次，模块实现该接口以实现定时服务
        """
        self._dispatch.broadcast("scheduler_job")

    def clear_cache(self) -> None:
        """
        清理缓存，模块实现该接口响应清理缓存事件
        """
        self._dispatch.broadcast("clear_cache")
