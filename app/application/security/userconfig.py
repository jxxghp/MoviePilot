"""用户个性化配置应用服务。"""

from __future__ import annotations

from typing import Any, Protocol


class UserConfigurationRepository(Protocol):
    """用户配置数据端口。"""

    def get(self, username: str, key: str) -> Any:
        """读取用户配置。"""

    def set(self, username: str, key: str, value: Any) -> Any:
        """写入用户配置。"""


class UserConfigurationService:
    """编排用户个性化配置读写。"""

    def __init__(self, repository: UserConfigurationRepository) -> None:
        """注入用户配置数据端口。"""
        self._repository = repository

    def get(self, username: str, key: str) -> Any:
        """读取用户配置。"""
        return self._repository.get(username=username, key=key)

    def set(self, username: str, key: str, value: Any) -> Any:
        """写入用户配置。"""
        return self._repository.set(username=username, key=key, value=value)


_configured_user_configuration: UserConfigurationService | None = None


def configure_user_configuration(service: UserConfigurationService) -> None:
    """由启动组合根登记用户配置服务。"""
    global _configured_user_configuration
    _configured_user_configuration = service


def get_configured_user_configuration() -> UserConfigurationService:
    """返回启动阶段登记的用户配置服务。"""
    if _configured_user_configuration is None:
        raise RuntimeError("用户配置服务尚未配置")
    return _configured_user_configuration
