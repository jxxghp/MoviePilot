from enum import Enum
from typing import Union, Optional

from app.core.cache import TTLCache
from app.schemas.types import ProgressKey
from app.utils.singleton import WeakSingleton


class ProgressHelper(metaclass=WeakSingleton):
    """
    处理进度辅助类
    """

    def __init__(self, key: Union[ProgressKey, str]):
        if isinstance(key, Enum):
            key = key.value
        self._key = key
        self._progress = TTLCache(maxsize=1024, ttl=24 * 60 * 60)

    def __reset(self):
        self._progress[self._key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        }

    def start(self):
        self.__reset()
        self._progress[self._key]['enable'] = True

    def end(self):
        if not self._progress.get(self._key):
            return
        self._progress[self._key].update(
            {
                "enable": False,
                "value": 100,
                "text": ""
            }
        )

    def update(self, value: Union[float, int] = None, text: Optional[str] = None, data: dict = None):
        if not self._progress.get(self._key).get('enable'):
            return
        if value:
            self._progress[self._key]['value'] = value
        if text:
            self._progress[self._key]['text'] = text
        if data:
            if not self._progress[self._key].get('data'):
                self._progress[self._key]['data'] = {}
            self._progress[self._key]['data'].update(data)

    def get(self) -> dict:
        return self._progress.get(self._key)
