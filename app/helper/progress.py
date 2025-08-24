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
        self._progress = TTLCache(region="progress", maxsize=1024, ttl=24 * 60 * 60)

    def __reset(self):
        """
        重置进度
        """
        self._progress[self._key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        }

    def start(self):
        """
        开始进度
        """
        self.__reset()
        current = self._progress.get(self._key)
        if not current:
            return
        current['enable'] = True
        self._progress[self._key] = current

    def end(self):
        """
        结束进度
        """
        current = self._progress.get(self._key)
        if not current:
            return
        current.update(
            {
                "enable": False,
                "value": 100,
                "text": ""
            }
        )
        self._progress[self._key] = current

    def update(self, value: Union[float, int] = None, text: Optional[str] = None, data: dict = None):
        """
        更新进度
        """
        current = self._progress.get(self._key)
        if not current or not current.get('enable'):
            return
        if value:
            current['value'] = value
        if text:
            current['text'] = text
        if data:
            if not current.get('data'):
                current['data'] = {}
            current['data'].update(data)
        self._progress[self._key] = current

    def get(self) -> dict:
        return self._progress.get(self._key)
