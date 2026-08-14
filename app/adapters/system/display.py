from pyvirtualdisplay import Display

from app.runtime.log import logger
from app.foundation.singleton import Singleton
from app.adapters.system.host import SystemUtils

import os


class DisplayHelper(metaclass=Singleton):
    """在容器环境中管理浏览器所需的虚拟显示。"""

    def __init__(self):
        """仅在 Docker 内启动虚拟显示服务。"""
        self._display = None
        if not SystemUtils.is_docker():
            return
        try:
            self._display = Display(visible=False, size=(1024, 768), extra_args=[os.environ['DISPLAY']])
            self._display.start()
        except Exception as err:
            logger.error(f"DisplayHelper init error: {str(err)}")

    def stop(self):
        """停止已经启动的虚拟显示服务。"""
        if self._display:
            logger.info("正在停止虚拟显示...")
            self._display.stop()
            logger.info("虚拟显示已停止")
