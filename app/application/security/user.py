"""用户管理用例。

该模块承接用户端点需要的异步用户操作。具体数据库访问由请求组合根注入，
避免 API 层同时承担 HTTP 编排和 ORM 适配职责。
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class UserRepository(Protocol):
    """用户用例所需的最小异步数据端口。"""

    async def async_list(self) -> list[Any]:
        """返回全部用户。"""

    async def async_get_by_name(self, name: str) -> Any | None:
        """按用户名返回用户。"""

    async def async_get_by_id(self, user_id: int) -> Any | None:
        """按用户 ID 返回用户。"""

    async def async_create(self, payload: dict[str, Any]) -> Any | None:
        """创建用户并返回持久化对象。"""

    async def async_update(self, user_id: int, payload: dict[str, Any]) -> Any | None:
        """更新用户并返回原用户对象。"""

    async def async_delete(self, user_id: int) -> None:
        """删除用户。"""

    async def async_update_otp_by_name(self, name: str, otp: bool, secret: str) -> None:
        """更新用户 OTP 状态。"""


class AsyncUnitOfWork(Protocol):
    """用户写用例所需的异步事务边界。"""

    async def commit(self) -> None:
        """提交用户写入。"""

    async def rollback(self) -> None:
        """回滚失败的用户写入。"""


class UserService:
    """用户管理应用服务。"""

    def __init__(
        self,
        repository: UserRepository,
        unit_of_work: AsyncUnitOfWork | None = None,
    ) -> None:
        """创建用户服务；旧独立仓储可暂不提供请求级 UoW。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    async def list(self) -> list[Any]:
        """返回用户列表。"""
        return await self._repository.async_list()

    async def get_by_name(self, name: str) -> Any | None:
        """按用户名查询用户。"""
        return await self._repository.async_get_by_name(name)

    async def get_by_id(self, user_id: int) -> Any | None:
        """按用户 ID 查询用户。"""
        return await self._repository.async_get_by_id(user_id)

    async def create(self, payload: dict[str, Any]) -> Any | None:
        """创建用户。"""
        return await self._write(lambda: self._repository.async_create(payload))

    async def update(self, user_id: int, payload: dict[str, Any]) -> Any | None:
        """更新用户。"""
        return await self._write(
            lambda: self._repository.async_update(user_id, payload)
        )

    async def delete(self, user_id: int) -> None:
        """删除用户。"""
        await self._write(lambda: self._repository.async_delete(user_id))

    async def update_otp(self, name: str, otp: bool, secret: str) -> None:
        """更新用户 OTP 状态。"""
        await self._write(
            lambda: self._repository.async_update_otp_by_name(name, otp, secret)
        )

    async def _write(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """执行用户写入，并在正式请求路径统一提交或回滚。"""
        try:
            result = await operation()
            if self._unit_of_work is not None:
                await self._unit_of_work.commit()
            return result
        except Exception:
            if self._unit_of_work is not None:
                await self._unit_of_work.rollback()
            raise


_configured_user_id_lookup: Callable[[int], Any | None] | None = None
_configured_user_name_lookup: Callable[[str], Any | None] | None = None
_configured_user_channel_lookup: Callable[..., str | None] | None = None


def configure_user_lookups(
    by_id: Callable[[int], Any | None],
    by_name: Callable[[str], Any | None],
    by_channel: Callable[..., str | None],
) -> None:
    """由启动组合根登记 ID、用户名和渠道身份查询能力。"""
    global _configured_user_id_lookup, _configured_user_name_lookup
    global _configured_user_channel_lookup
    _configured_user_id_lookup = by_id
    _configured_user_name_lookup = by_name
    _configured_user_channel_lookup = by_channel


def get_configured_user_id_lookup() -> Callable[[int], Any | None]:
    """返回启动阶段登记的按 ID 用户查询函数。"""
    if _configured_user_id_lookup is None:
        raise RuntimeError("按 ID 的用户查询能力尚未配置")
    return _configured_user_id_lookup


def get_configured_user_name_lookup() -> Callable[[str], Any | None]:
    """返回启动阶段登记的按用户名查询函数。"""
    if _configured_user_name_lookup is None:
        raise RuntimeError("按用户名的用户查询能力尚未配置")
    return _configured_user_name_lookup


def get_configured_user_channel_lookup() -> Callable[..., str | None]:
    """返回启动阶段登记的渠道身份到用户名查询函数。"""
    if _configured_user_channel_lookup is None:
        raise RuntimeError("渠道用户查询能力尚未配置")
    return _configured_user_channel_lookup
