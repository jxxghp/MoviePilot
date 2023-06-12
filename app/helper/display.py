from pyvirtualdisplay import Display

from app.log import logger
from app.utils.singleton import Singleton


class DisplayHelper(metaclass=Singleton):
    _display: Display = None

    def __init__(self):
        try:
            self._display = Display(visible=False, size=(1024, 768))
            self._display.start()
        except Exception as err:
            logger.error(f"DisplayHelper init error: {err}")

    def stop(self):
        if self._display:
            self._display.stop()
