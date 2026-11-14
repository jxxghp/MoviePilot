"""
按业务键保存并查询进度快照，支持同步与异步调用。
"""

from collections.abc import Mapping
from enum import Enum
from typing import Optional, TypedDict, Union

from app.runtime.cache import AsyncTTLCache, TTLCache
from app.runtime.localization import LocaleHelper
from app.schemas.types import ProgressKey


class ProgressSnapshot(TypedDict, total=False):
    """
    进度缓存的已声明字段；允许历史或部分快照缺少键。

    :ivar enable: 进度显示是否启用；未提供时由读取方按缺省值处理
    :ivar value: 当前进度百分比；键缺失表示尚无该项快照
    :ivar text: 当前步骤的中文说明；键缺失表示尚无文本
    :ivar text_i18n: 供前端翻译的步骤说明；键缺失时使用普通文本
    :ivar data: 步骤携带的附加业务数据；允许缺失
    """

    enable: bool
    value: float
    text: str
    text_i18n: str
    data: dict[str, object]


class ProgressHelper:
    """
    按业务键维护进度快照，供同步工作流和前端进度查询共享。
    """

    def __init__(self, key: Union[ProgressKey, str]) -> None:
        """
        为指定业务键绑定独立的进度缓存区域。
        """
        if isinstance(key, Enum):
            key = key.value
        self._key = key
        self._progress = TTLCache(region="progress", maxsize=1024, ttl=24 * 60 * 60)

    def __reset(self) -> None:
        """
        重置业务键对应的进度为停用、零百分比和空附加数据。
        """
        self._progress[self._key] = {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        }

    def start(self) -> None:
        """
        清空上一轮进度并启用当前业务键的进度显示。
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
            data: Optional[Mapping[str, object]] = None,
            value: Optional[Union[float, int]] = 100,
    ) -> None:
        """
        关闭进度显示，并写入最终说明与附加数据。

        尚无进度快照时直接返回。

        :param text: 最终步骤说明；None 或空字符串表示清空文本
        :param data: 合并到原快照的附加数据；None 表示保留原数据
        :param value: 最终百分比，限制在 0 至 100；默认为 100，None 表示保留原进度
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
            data: Optional[Mapping[str, object]] = None,
    ) -> None:
        """
        更新已启用的进度快照，未开始或已结束的进度不作修改。

        :param value: 新百分比，限制在 0 至 100；None 表示保留原进度
        :param text: 新步骤说明；None 表示保留原文本
        :param data: 合并到原快照的附加数据；None 表示保留原数据
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

    def get(self, locale: Optional[str] = None) -> Optional[ProgressSnapshot]:
        """
        获取当前进度，并按语言补充前端展示字段。

        :param locale: 目标语言，未传入时使用当前请求上下文语言
        :return: 当前进度字典
        """
        current: ProgressSnapshot | None = self._progress.get(self._key)
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
    按业务键异步维护进度快照，与同步进度服务共享同一缓存区域。

    内存后端共享进程内存储，Redis 后端共享键空间；异步入口避免同步缓存 I/O 阻塞事件循环。
    """

    def __init__(self, key: Union[ProgressKey, str]) -> None:
        """
        为指定业务键绑定独立的异步进度缓存区域。
        """
        if isinstance(key, Enum):
            key = key.value
        self._key = key
        self._progress = AsyncTTLCache(region="progress", maxsize=1024, ttl=24 * 60 * 60)

    async def __reset(self) -> None:
        """
        重置业务键对应的进度为停用、零百分比和空附加数据。
        """
        await self._progress.set(self._key, {
            "enable": False,
            "value": 0,
            "text": "请稍候...",
            "data": {}
        })

    async def start(self) -> None:
        """
        清空上一轮进度并启用当前业务键的进度显示。
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
            data: Optional[Mapping[str, object]] = None,
            value: Optional[Union[float, int]] = 100,
    ) -> None:
        """
        关闭进度显示，并写入最终说明与附加数据。

        即使原快照读取失败也创建终态快照，避免任务持续显示运行中。

        :param text: 最终步骤说明；None 或空字符串表示清空文本
        :param data: 合并到原快照的附加数据；None 表示保留原数据
        :param value: 最终百分比，限制在 0 至 100；默认为 100，None 表示保留原进度
        """
        current = await self._progress.get(self._key)
        if not current:
            # 读取快照失败时仍需落地终态，避免 Redis 短暂故障让任务永久显示运行中。
            current = {
                "enable": False,
                "value": 0,
                "text": "",
                "data": {},
            }
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
            data: Optional[Mapping[str, object]] = None,
    ) -> None:
        """
        更新已启用的进度快照，未开始或已结束的进度不作修改。

        :param value: 新百分比，限制在 0 至 100；None 表示保留原进度
        :param text: 新步骤说明；None 表示保留原文本
        :param data: 合并到原快照的附加数据；None 表示保留原数据
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

    async def get(self, locale: Optional[str] = None) -> Optional[ProgressSnapshot]:
        """
        获取当前进度，并按语言补充前端展示字段。

        :param locale: 目标语言，未传入时使用当前请求上下文语言
        :return: 当前进度字典
        """
        current: ProgressSnapshot | None = await self._progress.get(self._key)
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
