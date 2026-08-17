"""插件运行时持久化端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
PluginDataDeleter = Callable[[str], Any]


def _empty_read(_key: Any) -> Any:
    """组合根尚未装配时返回空配置。"""
    return None


def _ignore_write(_key: Any, _value: Any) -> None:
    """组合根尚未装配时忽略同步配置写入。"""


async def _ignore_async_write(_key: Any, _value: Any) -> None:
    """组合根尚未装配时忽略异步配置写入。"""


def _ignore_delete(_key: Any) -> bool:
    """组合根尚未装配时报告配置未删除。"""
    return False


def _ignore_plugin_data_delete(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据删除。"""


class PluginStorage:
    """封装插件运行时所需的最小持久化能力。"""

    def __init__(
            self,
            *,
            read: ConfigReader = _empty_read,
            write: ConfigWriter = _ignore_write,
            async_write: AsyncConfigWriter = _ignore_async_write,
            delete: ConfigDeleter = _ignore_delete,
            delete_data: PluginDataDeleter = _ignore_plugin_data_delete,
    ) -> None:
        """保存由启动组合根提供的读写函数。"""
        self._read = read
        self._write = write
        self._async_write = async_write
        self._delete = delete
        self._delete_data = delete_data

    def read(self, key: Any) -> Any:
        """读取插件运行时配置。"""
        return self._read(key)

    def write(self, key: Any, value: Any) -> Any:
        """同步保存插件运行时配置。"""
        return self._write(key, value)

    async def async_write(self, key: Any, value: Any) -> Any:
        """异步保存插件运行时配置。"""
        return await self._async_write(key, value)

    def delete(self, key: Any) -> bool:
        """删除插件运行时配置。"""
        return self._delete(key)

    def delete_data(self, plugin_id: str) -> Any:
        """删除指定插件的业务数据。"""
        return self._delete_data(plugin_id)


_plugin_storage = PluginStorage()


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
