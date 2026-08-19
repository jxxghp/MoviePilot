from enum import Enum
from typing import Optional, Union

from app.runtime.cache import AsyncTTLCache, TTLCache
from app.runtime.localization import LocaleHelper
from app.schemas.types import ProgressKey


class ProgressHelper:
    """
    处理进度辅助类
    """

    def __init__(self, key: Union[ProgressKey, str]) -> None:
        """为指定业务键绑定独立的进度缓存区域。"""
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


class AsyncProgressHelper:
    """
    处理进度辅助类（异步）

    与 ProgressHelper 共用同一个进度 region：内存后端共享进程内存储，
    Redis 后端共享同一键空间，因此同步写入、异步读取（或反之）均互通。
    供事件循环上的异步调用方使用，避免同步缓存后端阻塞事件循环。
    """

    def __init__(self, key: Union[ProgressKey, str]) -> None:
        """为指定业务键绑定独立的异步进度缓存区域。"""
        if isinstance(key, Enum):
            key = key.value
        self._key = key
        self._progress = AsyncTTLCache(region="progress", maxsize=1024, ttl=24 * 60 * 60)

    async def __reset(self) -> None:
        """
        重置进度
        """
        await self._progress.set(self._key, {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        })

    async def start(self) -> None:
        """
        开始进度
        """
        await self.__reset()
        current = await self._progress.get(self._key)
        if not current:
            return
        current['enable'] = True
        await self._progress.set(self._key, current)

    async def end(
            self,
            text: Optional[str] = "",
            data: Optional[dict] = None,
            value: Optional[Union[float, int]] = 100,
    ) -> None:
        """
        结束进度
        """
        current = await self._progress.get(self._key)
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
        await self._progress.set(self._key, current)

    async def update(
            self,
            value: Optional[Union[float, int]] = None,
            text: Optional[str] = None,
            data: Optional[dict] = None,
    ) -> None:
        """
        更新进度
        """
        current = await self._progress.get(self._key)
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
        await self._progress.set(self._key, current)

    async def get(self, locale: Optional[str] = None) -> Optional[dict]:
        """
        获取当前进度，并按语言补充前端展示字段。

        :param locale: 目标语言，未传入时使用当前请求上下文语言
        :return: 当前进度字典
        """
        current = await self._progress.get(self._key)
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
