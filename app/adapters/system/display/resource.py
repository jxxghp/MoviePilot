"""虚拟显示进程的托管资源实现。"""

from __future__ import annotations

import os
from typing import Any, Optional

from app.adapters.system.host import SystemUtils
from app.runtime.log import logger


class VirtualDisplayResource:
    """按需拥有一个容器内虚拟显示进程。"""

    def __init__(self) -> None:
        self._display: Optional[Any] = None

    @property
    def display(self) -> Optional[Any]:
        """返回当前拥有的显示对象；未启动或已停止时为 None。"""
        return self._display

    def start(self) -> None:
        """仅在容器环境启动虚拟显示，重复启动保持幂等。"""
        if self._display is not None or not SystemUtils.is_docker():
            return
        from pyvirtualdisplay import Display

        display = Display(
            visible=False,
            size=(1024, 768),
            extra_args=[os.environ["DISPLAY"]],
        )
        self._display = display
        display.start()

    def stop(self) -> None:
        """停止当前资源拥有的显示进程，失败时保留句柄供 Runtime 重试。"""
        display = self._display
        if display is None:
            return
        logger.info("正在停止虚拟显示...")
        display.stop()
        self._display = None
        logger.info("虚拟显示已停止")
