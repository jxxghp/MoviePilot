"""系统配置应用服务与组合根注入点。"""

from __future__ import annotations

from typing import Any, Protocol


class ConfigurationRepository(Protocol):
    """配置服务所需的最小持久化端口。"""

    def get(self, key: Any = None) -> Any:
        """读取配置。"""

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""

    async def async_get(self, key: Any = None) -> Any:
        """异步读取配置。"""

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """异步写入配置。"""

    def delete(self, key: Any) -> Any:
        """删除配置。"""


class SystemConfigService:
    """系统配置读写应用服务。"""

    def __init__(self, repository: ConfigurationRepository) -> None:
        """注入配置数据端口。"""
        self._repository = repository

    def get(self, key: Any = None) -> Any:
        """读取配置。"""
        return self._repository.get(key)

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""
        return self._repository.set(key, value)

    async def async_get(self, key: Any = None) -> Any:
        """异步读取配置。"""
        return await self._repository.async_get(key)

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """异步写入配置。"""
        return await self._repository.async_set(key, value)

    def delete(self, key: Any) -> Any:
        """删除配置。"""
        return self._repository.delete(key)


_configured_system_config: SystemConfigService | None = None


def configure_system_config(service: SystemConfigService) -> None:
    """由启动组合根登记系统配置服务。"""
    global _configured_system_config
    _configured_system_config = service


def get_configured_system_config() -> SystemConfigService:
    """返回启动阶段登记的系统配置服务。"""
    if _configured_system_config is None:
        raise RuntimeError("系统配置服务尚未配置")
    return _configured_system_config
