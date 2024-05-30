from enum import Enum

from app.schemas.types import ProgressKey
from app.utils.singleton import Singleton


class ProgressHelper(metaclass=Singleton):
    _process_detail: dict[str, dict] = {}

    def __init__(self):
        self._process_detail = {}

    def init_config(self):
        pass

    def __reset(self, key: ProgressKey | str):
        if isinstance(key, Enum):
            key = key.value
        self._process_detail[key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候..."
        }

    def start(self, key: ProgressKey | str):
        self.__reset(key)
        if isinstance(key, Enum):
            key = key.value
        self._process_detail[key]['enable'] = True

    def end(self, key: ProgressKey | str):
        if isinstance(key, Enum):
            key = key.value
        if not self._process_detail.get(key):
            return
        self._process_detail[key]['enable'] = False

    def update(self, key: ProgressKey | str, value: float = None, text: str = None):
        if isinstance(key, Enum):
            key = key.value
        if not self._process_detail.get(key, {}).get('enable'):
            return
        if value:
            self._process_detail[key]['value'] = value
        if text:
            self._process_detail[key]['text'] = text

    def get(self, key: ProgressKey | str) -> dict:
        if isinstance(key, Enum):
            key = key.value
        return self._process_detail.get(key)
