from enum import Enum
from typing import Union, Dict, Optional

from app.schemas.types import ProgressKey
from app.utils.singleton import Singleton


class ProgressHelper(metaclass=Singleton):
    """
    进度条帮助类
    """
    _process_detail: Dict[str, dict] = {}

    def __init__(self):
        """
        初始化进度条帮助类
        """
        self._process_detail = {}

    def init_config(self):
        pass

    def __reset(self, key: Union[ProgressKey, str]):
        """
        重置进度条状态
        """
        if isinstance(key, Enum):
            key = key.value
        self._process_detail[key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候..."
        }

    def start(self, key: Union[ProgressKey, str]):
        """
        开始一个新的进度条
        """
        self.__reset(key)
        if isinstance(key, Enum):
            key = key.value
        self._process_detail[key]['enable'] = True

    def end(self, key: Union[ProgressKey, str]):
        """
        结束一个进度条
        """
        if isinstance(key, Enum):
            key = key.value
        if not self._process_detail.get(key):
            return
        self._process_detail[key] = {
            "enable": False,
            "value": 100,
            "text": "正在处理..."
        }

    def update(self, key: Union[ProgressKey, str], value: Union[float, int] = None, text: Optional[str] = None):
        """
        更新进度条状态
        """
        if isinstance(key, Enum):
            key = key.value
        if not self._process_detail.get(key, {}).get('enable'):
            return
        if value:
            self._process_detail[key]['value'] = value
        if text:
            self._process_detail[key]['text'] = text

    def get(self, key: Union[ProgressKey, str]) -> Optional[dict]:
        """
        获取进度条状态。

        """
        if isinstance(key, Enum):
            key = key.value
        detail = self._process_detail.get(key)
        if detail and not detail.get("enable"):
            return self._process_detail.pop(key, None)
        return detail
