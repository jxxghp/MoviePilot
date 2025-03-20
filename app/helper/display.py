from pyvirtualdisplay import Display

from app.log import logger
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils

import os


class DisplayHelper(metaclass=Singleton):
    _display: Display = None

    def __init__(self):
        if not SystemUtils.is_docker():
            return
        try:
            self._display = Display(visible=False, size=(1024, 768), extra_args=[os.environ['DISPLAY']])
            self._display.start()
        except Exception as err:
            logger.error(f"DisplayHelper init error: {str(err)}")

    def stop(self):
        if self._display:
            logger.info("正在停止虚拟显示...")
            self._display.stop()
            logger.info("虚拟显示已停止")
