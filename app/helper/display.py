from pyvirtualdisplay import Display

from app.log import logger
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class DisplayHelper(metaclass=Singleton):
    _display: Display = None

    def __init__(self):
        if not SystemUtils.is_docker():
            return
        try:
            self._display = Display(visible=False, size=(1024, 768))
            self._display.start()
        except Exception as err:
            logger.error(f"DisplayHelper init error: {str(err)}")

    def stop(self):
        if self._display:
            self._display.stop()
