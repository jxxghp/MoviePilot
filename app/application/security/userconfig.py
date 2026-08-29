"""用户个性化配置应用服务。"""

from __future__ import annotations

from functools import partial
from typing import Optional, Protocol, Union

from app.application.database import AsyncDatabaseExecutor
from app.schemas.common import JsonData
from app.schemas.types import UserConfigKey


class UserConfigurationRepository(Protocol):
    """用户配置数据端口。"""

    def get(
        self,
        username: str,
        key: Union[str, UserConfigKey],
    ) -> JsonData:
        """读取用户配置。"""

    def set(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> None:
        """写入用户配置。"""

    def publish_rename(self, previous_name: str, current_name: str) -> None:
        """在用户改名提交后迁移进程级配置快照。"""

    def publish_delete(self, username: str) -> None:
        """在用户删除提交后移除进程级配置快照。"""


class UserConfigurationService:
    """编排用户个性化配置读写。"""

    def __init__(
        self,
        repository: UserConfigurationRepository,
        *,
        async_executor: Optional[AsyncDatabaseExecutor] = None,
    ) -> None:
        """注入用户配置数据端口及可选的异步事务执行能力。"""
        self._repository = repository
        self._async_executor = async_executor

    def get(
        self,
        username: str,
        key: Union[str, UserConfigKey],
    ) -> JsonData:
        """读取用户配置。"""
        return self._repository.get(username=username, key=key)

    def set(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> None:
        """写入用户配置。"""
        self._repository.set(username=username, key=key, value=value)

    async def async_set(
        self,
        username: str,
        key: Union[str, UserConfigKey],
        value: JsonData,
    ) -> None:
        """异步写入用户配置，并等待数据库提交或回滚完成。"""
        if self._async_executor is None:
            raise RuntimeError("用户配置异步数据库执行端口尚未配置")
        await self._async_executor.run(partial(self._repository.set, username=username, key=key, value=value))

    async def rename(self, previous_name: str, current_name: str) -> None:
        """异步发布已提交的用户名配置迁移。"""
        if self._async_executor is None:
            raise RuntimeError("用户配置异步数据库执行端口尚未配置")
        await self._async_executor.run(partial(self._repository.publish_rename, previous_name, current_name))

    async def delete(self, username: str) -> None:
        """异步发布已提交的用户名配置删除。"""
        if self._async_executor is None:
            raise RuntimeError("用户配置异步数据库执行端口尚未配置")
        await self._async_executor.run(partial(self._repository.publish_delete, username))


_configured_user_configuration: Optional[UserConfigurationService] = None


def configure_user_configuration(service: UserConfigurationService) -> None:
    """由启动组合根登记用户配置服务。"""
    global _configured_user_configuration
    _configured_user_configuration = service


def reset_user_configuration() -> None:
    """清除当前 lifespan 的用户配置服务，避免重复启动复用旧实例。"""
    global _configured_user_configuration
    _configured_user_configuration = None


def get_configured_user_configuration() -> UserConfigurationService:
    """返回启动阶段登记的用户配置服务。"""
    if _configured_user_configuration is None:
        raise RuntimeError("用户配置服务尚未配置")
    return _configured_user_configuration
