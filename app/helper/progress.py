from enum import Enum
from typing import Optional, Union

from app.core.cache import TTLCache
from app.helper.locale import LocaleHelper
from app.schemas.types import ProgressKey


class ProgressHelper:
    """
    处理进度辅助类
    """

    def __init__(self, key: Union[ProgressKey, str]) -> None:
        if isinstance(key, Enum):
            key = key.value
        self._key = key
        self._progress = TTLCache(region="progress", maxsize=1024, ttl=24 * 60 * 60)

    def __reset(self) -> None:
        """
        重置进度
        """
        self._progress[self._key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        }

    def start(self) -> None:
        """
        开始进度
        """
        self.__reset()
        current = self._progress.get(self._key)
        if not current:
            return
        current['enable'] = True
        self._progress[self._key] = current

    def end(
            self,
            text: Optional[str] = "",
            data: Optional[dict] = None,
            value: Optional[Union[float, int]] = 100,
    ) -> None:
        """
        结束进度
        """
        current = self._progress.get(self._key)
        if not current:
            return
        if data is not None:
            if not current.get('data'):
                current['data'] = {}
            current['data'].update(data)
        current["enable"] = False
        if value is not None:
            current["value"] = max(min(float(value), 100), 0)
        current["text"] = text or ""
        self._progress[self._key] = current

    def update(
            self,
            value: Optional[Union[float, int]] = None,
            text: Optional[str] = None,
            data: Optional[dict] = None,
    ) -> None:
        """
        更新进度
        """
        current = self._progress.get(self._key)
        if not current or not current.get('enable'):
            return
        if value is not None:
            current['value'] = max(min(float(value), 100), 0)
        if text is not None:
            current['text'] = text
        if data is not None:
            if not current.get('data'):
                current['data'] = {}
            current['data'].update(data)
        self._progress[self._key] = current

    def get(self, locale: Optional[str] = None) -> Optional[dict]:
        """
        获取当前进度，并按语言补充前端展示字段。

        :param locale: 目标语言，未传入时使用当前请求上下文语言
        :return: 当前进度字典
        """
        current = self._progress.get(self._key)
        if not current:
            return current

        detail = current.copy()
        text = detail.get("text")
        if isinstance(text, str):
            detail["text_i18n"] = LocaleHelper.translate_text(text, locale=locale)

        data = detail.get("data")
        if isinstance(data, dict):
            localized_data = data.copy()
            error = localized_data.get("error")
            message = localized_data.get("message")
            if isinstance(error, str):
                localized_data["error_i18n"] = LocaleHelper.translate_text(
                    error, locale=locale
                )
            if isinstance(message, str):
                localized_data["message_i18n"] = LocaleHelper.translate_text(
                    message, locale=locale
                )
            detail["data"] = localized_data
        return detail
