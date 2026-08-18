"""插件运行时持久化端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
PluginDataDeleter = Callable[[str], Any]
PluginExists = Callable[[str], bool]


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


class PluginConfigStore:
    """封装插件配置键、存在性和强制删除规则。"""

    def __init__(
        self,
        *,
        storage: Callable[[], "PluginStorage"],
        plugin_exists: PluginExists,
        key_prefix: str = "plugin.%s",
    ) -> None:
        """保存持久化端口和运行态插件查询端口。"""
        self._storage = storage
        self._plugin_exists = plugin_exists
        self._key_prefix = key_prefix

    def _key(self, plugin_id: str) -> str:
        """构造插件配置在统一配置存储中的键。"""
        return self._key_prefix % plugin_id

    def read(self, plugin_id: str) -> dict:
        """读取配置并过滤历史空键。"""
        if not self._plugin_exists(plugin_id):
            return {}
        config = self._storage().read(self._key(plugin_id))
        return {
            key: value
            for key, value in (config or {}).items()
            if key
        }

    def write(self, plugin_id: str, config: dict, force: bool = False) -> bool:
        """保存配置，默认拒绝不存在插件的配置写入。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        self._storage().write(self._key(plugin_id), config)
        return True

    async def async_write(
        self,
        plugin_id: str,
        config: dict,
        force: bool = False,
    ) -> bool:
        """异步保存配置并保持同步写入的存在性规则。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        await self._storage().async_write(self._key(plugin_id), config)
        return True

    def delete(self, plugin_id: str, force: bool = False) -> bool:
        """删除配置并保持停止插件后的强制删除能力。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        return self._storage().delete(self._key(plugin_id))

    def delete_data(self, plugin_id: str, force: bool = False) -> bool:
        """删除插件业务数据并保持旧的布尔结果合同。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        self._storage().delete_data(plugin_id)
        return True


_plugin_storage = PluginStorage()


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
