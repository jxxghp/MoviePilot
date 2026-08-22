"""插件运行时持久化端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
PluginDataDeleter = Callable[[str], Any]
PluginConfigReader = Callable[..., Any]
PluginConfigWriter = Callable[[str, Any], Any]
AsyncPluginConfigWriter = Callable[[str, Any], Awaitable[Any]]
PluginConfigDeleter = Callable[[str], bool]
PluginInstanceLister = Callable[[str], list]


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


def _empty_read_config(_plugin_id: str, _instance_id: Any = None) -> Any:
    """组合根尚未装配时返回空的插件实例配置。"""
    return None


def _empty_list_instances(_plugin_id: str) -> list:
    """组合根尚未装配时返回空的插件实例清单。"""
    return []


def _ignore_write_config(_plugin_id: str, _value: Any) -> None:
    """组合根尚未装配时忽略插件实例配置的同步写入。"""


async def _ignore_async_write_config(_plugin_id: str, _value: Any) -> None:
    """组合根尚未装配时忽略插件实例配置的异步写入。"""


def _ignore_delete_config(_plugin_id: str) -> bool:
    """组合根尚未装配时报告插件实例配置未删除。"""
    return False


class PluginStorage:
    """封装插件运行时所需的最小持久化能力。

    ``read``/``write``/``async_write``/``delete`` 是按任意键读写的通用配置通道，
    承载已安装插件清单、插件文件夹分组等非插件业务配置的持久化。插件自身的实例
    配置（业务配置字典）改走 ``read_config``/``write_config``/``async_write_config``/
    ``delete_config``，按 ``(插件 ID, 实例标识)`` 定位一行；``list_instances`` 给出
    某插件已登记的实例清单。
    """

    def __init__(
            self,
            *,
            read: ConfigReader = _empty_read,
            write: ConfigWriter = _ignore_write,
            async_write: AsyncConfigWriter = _ignore_async_write,
            delete: ConfigDeleter = _ignore_delete,
            delete_data: PluginDataDeleter = _ignore_plugin_data_delete,
            read_config: PluginConfigReader = _empty_read_config,
            write_config: PluginConfigWriter = _ignore_write_config,
            async_write_config: AsyncPluginConfigWriter = _ignore_async_write_config,
            delete_config: PluginConfigDeleter = _ignore_delete_config,
            list_instances: PluginInstanceLister = _empty_list_instances,
    ) -> None:
        """保存由启动组合根提供的读写函数。"""
        self._read = read
        self._write = write
        self._async_write = async_write
        self._delete = delete
        self._delete_data = delete_data
        self._read_config = read_config
        self._write_config = write_config
        self._async_write_config = async_write_config
        self._delete_config = delete_config
        self._list_instances = list_instances

    def read(self, key: Any) -> Any:
        """按键读取插件运行时配置。"""
        return self._read(key)

    def write(self, key: Any, value: Any) -> Any:
        """按键同步保存插件运行时配置。"""
        return self._write(key, value)

    async def async_write(self, key: Any, value: Any) -> Any:
        """按键异步保存插件运行时配置。"""
        return await self._async_write(key, value)

    def delete(self, key: Any) -> bool:
        """按键删除插件运行时配置。"""
        return self._delete(key)

    def delete_data(self, plugin_id: str) -> Any:
        """删除指定插件的业务数据。"""
        return self._delete_data(plugin_id)

    def read_config(self, plugin_id: str, instance_id: Optional[str] = None) -> Any:
        """读取指定插件实例的业务配置。

        :param plugin_id: 插件 ID
        :param instance_id: 实例标识，为空时取默认实例
        :return: 该实例的业务配置，未登记时为 None
        """
        # 默认实例只传插件 ID，只认单参数的读取实现继续可用
        if not instance_id or instance_id == DEFAULT_INSTANCE_ID:
            return self._read_config(plugin_id)
        return self._read_config(plugin_id, instance_id)

    def list_instances(self, plugin_id: str) -> list:
        """列出指定插件已登记的全部实例标识。

        :param plugin_id: 插件 ID
        :return: 实例标识列表；一条实例配置都没有时为空列表
        """
        return list(self._list_instances(plugin_id) or [])

    def write_config(self, plugin_id: str, value: Any) -> Any:
        """同步保存指定插件默认实例的业务配置。"""
        return self._write_config(plugin_id, value)

    async def async_write_config(self, plugin_id: str, value: Any) -> Any:
        """异步保存指定插件默认实例的业务配置。"""
        return await self._async_write_config(plugin_id, value)

    def delete_config(self, plugin_id: str) -> bool:
        """删除指定插件默认实例的业务配置。"""
        return self._delete_config(plugin_id)


_plugin_storage = PluginStorage()


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
